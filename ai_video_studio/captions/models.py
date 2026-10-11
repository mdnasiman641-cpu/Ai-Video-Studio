"""Whisper (faster-whisper / CTranslate2) model management.

Models are only downloaded when the user agrees (the Generate Captions dialog asks, and
Settings → Speech recognition models has Download buttons). They are stored per user in
``<data folder>\\models\\whisper\\<name>`` (outside the installation folder, so upgrades keep
them) and used from there offline. Each file is checked against the size and checksum that
Hugging Face publishes for it (SHA-256 for large files, the git object id for small ones).
Models downloaded by earlier versions into the Hugging Face cache layout are reused.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ai_video_studio.core import paths
from ai_video_studio.core.download import DownloadError, Opener, download_file, fetch_json
from ai_video_studio.core.fs import remove_tree

log = logging.getLogger(__name__)

HF_URL = "https://huggingface.co"


@dataclass(frozen=True)
class WhisperModelSpec:
    name: str
    repo: str
    approx_mb: int


WHISPER_MODELS: dict[str, WhisperModelSpec] = {s.name: s for s in (
    WhisperModelSpec("tiny", "Systran/faster-whisper-tiny", 75),
    WhisperModelSpec("base", "Systran/faster-whisper-base", 145),
    WhisperModelSpec("small", "Systran/faster-whisper-small", 485),
    WhisperModelSpec("medium", "Systran/faster-whisper-medium", 1530),
    WhisperModelSpec("large-v3", "Systran/faster-whisper-large-v3", 3090),
    WhisperModelSpec("large-v3-turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo", 1620),
)}
SKIP_FILES = (".gitattributes", "README.md")
_download_lock = threading.Lock()  # one download at a time (two caption jobs may want the same model)


class ModelMissingError(DownloadError):
    pass


def whisper_root() -> Path:
    path = paths.models_dir() / "whisper"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _complete(folder: Path) -> bool:
    if not ((folder / "model.bin").is_file() and (folder / "config.json").is_file()):
        return False
    return (folder / "tokenizer.json").is_file() or any(folder.glob("vocabulary.*"))


def installed_path(name: str) -> Path | None:
    """Folder of a complete downloaded model, or None."""
    spec = WHISPER_MODELS[name]
    folder = whisper_root() / name
    if _complete(folder):
        return folder
    legacy = paths.models_dir() / f"models--{spec.repo.replace('/', '--')}" / "snapshots"
    if legacy.is_dir():  # downloaded by faster-whisper itself in v0.2–v0.3
        for snapshot in sorted(legacy.iterdir(), reverse=True):
            if _complete(snapshot):
                return snapshot
    return None


def folder_size(folder: Path) -> int:
    return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())


def model_status(name: str) -> dict:
    path = installed_path(name)
    spec = WHISPER_MODELS[name]
    return {"name": name, "repo": spec.repo, "approx_mb": spec.approx_mb, "downloaded": path is not None,
            "path": str(path) if path else None, "size_mb": round(folder_size(path) / 1e6) if path else None}


def remove_model(name: str) -> bool:
    """Delete a downloaded model (both the current and the legacy location)."""
    spec = WHISPER_MODELS[name]
    ok = remove_tree(whisper_root() / name)
    legacy = paths.models_dir() / f"models--{spec.repo.replace('/', '--')}"
    if legacy.exists():
        ok = remove_tree(legacy) and ok
    return ok


def download_model(
    name: str,
    progress: Callable[[float, str], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
    opener: Opener | None = None,
    base_url: str = HF_URL,
) -> Path:
    """Download (or finish downloading) ``name``; returns the model folder."""
    if name not in WHISPER_MODELS:
        raise ModelMissingError(f"Unknown Whisper model '{name}'.")
    with _download_lock:
        return _download(name, progress, check_cancelled, opener, base_url)


def _download(name, progress, check_cancelled, opener, base_url) -> Path:
    existing = installed_path(name)
    if existing is not None:
        return existing
    spec = WHISPER_MODELS[name]
    info = fetch_json(f"{base_url}/api/models/{spec.repo}/revision/main?blobs=true", opener=opener)
    revision = info.get("sha") or "main"
    files = [s for s in info.get("siblings", []) if s.get("rfilename") not in SKIP_FILES]
    if not any(f["rfilename"] == "model.bin" for f in files):
        raise DownloadError(f"The model repository {spec.repo} does not contain a CTranslate2 model.")
    total = sum(int(f.get("size") or 0) for f in files) or spec.approx_mb * 1_000_000
    root = whisper_root()
    free = shutil.disk_usage(root).free
    if free < total * 1.05:
        raise DownloadError(f"Not enough free disk space for the '{name}' model: {total / 1e9:.1f} GB needed, "
                            f"{free / 1e9:.1f} GB free on the drive of {root}.")
    staging = root / f".{name}.partial"  # kept on failure so the download can resume
    done_before = 0
    for entry in files:
        filename = entry["rfilename"]
        size = int(entry["size"]) if entry.get("size") is not None else None
        lfs = entry.get("lfs") or {}

        def on_progress(done: int, _total, base=done_before, filename=filename) -> None:
            if progress:
                progress(min(1.0, (base + done) / total),
                         f"Downloading Whisper '{name}': {(base + done) / 1e6:.0f} / {total / 1e6:.0f} MB ({filename})")

        download_file(f"{base_url}/{spec.repo}/resolve/{revision}/{filename}", staging / filename, size=size,
                      sha256=lfs.get("sha256"), git_sha1=None if lfs else entry.get("blobId"),
                      progress=on_progress, check_cancelled=check_cancelled, opener=opener)
        done_before += size or 0
    (staging / "download.json").write_text(json.dumps({"repo": spec.repo, "revision": revision,
                                                       "files": [f["rfilename"] for f in files]}, indent=2))
    target = root / name
    if target.exists():
        remove_tree(target)
    staging.replace(target)
    log.info("Whisper model downloaded", extra={"model": name, "revision": revision, "bytes": total})
    return target

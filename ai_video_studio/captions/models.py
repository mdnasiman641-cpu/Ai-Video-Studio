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


# -- safe inspection ---------------------------------------------------------------------------
# Model folders are inspected while the main window is built (Settings → Speech recognition models),
# so inspecting must never raise. Hugging Face cache snapshots usually contain symbolic links into
# ``blobs``; Windows can refuse to follow them (WinError 448 "untrusted mount point", or access
# denied). Only filesystem errors (OSError) are caught here, and each becomes a reported problem.

WIN_UNTRUSTED_MOUNT_POINT = 448
REQUIRED_FILES = ("model.bin", "config.json")
MODEL_STATES = {
    "installed": "Downloaded",
    "not_downloaded": "Not downloaded",
    "incomplete": "Incomplete download",
    "inaccessible": "Cannot be read",
}


def describe_os_error(path: Path, exc: OSError) -> str:
    winerror = getattr(exc, "winerror", None)
    if winerror == WIN_UNTRUSTED_MOUNT_POINT:
        return (f"Windows refused to follow a link in {path} (WinError 448: untrusted mount point). "
                "Model caches made by earlier versions contain such links.")
    if isinstance(exc, PermissionError):
        return f"Access to {path} was denied ({exc.strerror or exc})."
    return f"{path} cannot be read: {exc.strerror or exc}"


def _check_file(path: Path, problems: list[str]) -> bool:
    try:
        return path.is_file()
    except OSError as exc:
        problems.append(describe_os_error(path, exc))
        return False


def _has_vocabulary(folder: Path, problems: list[str]) -> bool:
    if _check_file(folder / "tokenizer.json", problems):
        return True
    try:
        return any(_check_file(f, problems) for f in folder.glob("vocabulary.*"))
    except OSError as exc:
        problems.append(describe_os_error(folder, exc))
        return False


def inspect_folder(folder: Path) -> tuple[str, list[str]]:
    """``(state, problems)`` for a model folder; state is a key of :data:`MODEL_STATES`."""
    problems: list[str] = []
    try:
        exists = folder.is_dir()
    except OSError as exc:
        return "inaccessible", [describe_os_error(folder, exc)]
    if not exists:
        return "not_downloaded", []
    present = [_check_file(folder / name, problems) for name in REQUIRED_FILES]
    vocabulary = _has_vocabulary(folder, problems)
    if all(present) and vocabulary:
        return "installed", []
    if problems:
        return "inaccessible", problems
    missing = [n for n, ok in zip(REQUIRED_FILES, present) if not ok] + ([] if vocabulary else ["tokenizer"])
    return "incomplete", [f"{folder} is missing {', '.join(missing)}."]


def _complete(folder: Path) -> bool:
    return inspect_folder(folder)[0] == "installed"


def legacy_cache(name: str) -> Path:
    """The Hugging Face cache folder that faster-whisper used in v0.2–v0.3."""
    return paths.models_dir() / f"models--{WHISPER_MODELS[name].repo.replace('/', '--')}"


def legacy_snapshots(name: str, problems: list[str] | None = None) -> list[Path]:
    snapshots = legacy_cache(name) / "snapshots"
    problems = [] if problems is None else problems
    try:
        if not snapshots.is_dir():
            return []
        return sorted((s for s in snapshots.iterdir()), reverse=True)
    except OSError as exc:
        problems.append(describe_os_error(snapshots, exc))
        return []


@dataclass
class ModelInspection:
    name: str
    state: str                      # see MODEL_STATES
    path: Path | None               # usable model folder when state == "installed"
    problems: list[str]
    importable: Path | None = None  # cache snapshot whose files can be copied without following links


def inspect_model(name: str) -> ModelInspection:
    """Where the model is and whether it can be used. Never raises for filesystem problems."""
    folder = whisper_root() / name
    state, problems = inspect_folder(folder)
    if state == "installed":
        return ModelInspection(name, "installed", folder, [])
    all_problems = list(problems)
    for snapshot in legacy_snapshots(name, all_problems):
        snap_state, snap_problems = inspect_folder(snapshot)
        if snap_state == "installed":
            return ModelInspection(name, "installed", snapshot, [])
        all_problems += snap_problems
        if snap_state in ("inaccessible", "incomplete") and _plan_import(snapshot) is not None:
            return ModelInspection(name, "inaccessible", None, all_problems, importable=snapshot)
    if state == "not_downloaded" and all_problems:
        state = "inaccessible"
    return ModelInspection(name, state, None, all_problems)


def installed_path(name: str) -> Path | None:
    """Folder of a complete, readable model, or None."""
    return inspect_model(name).path


def folder_size(folder: Path) -> int:
    total = 0
    try:
        for f in folder.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def model_status(name: str) -> dict:
    info = inspect_model(name)
    spec = WHISPER_MODELS[name]
    problem = " ".join(dict.fromkeys(info.problems))
    if info.importable is not None:
        problem += (" A copy in the old cache can be imported (no download): click Download. The old cache "
                    "is not changed.")
    return {"name": name, "repo": spec.repo, "approx_mb": spec.approx_mb, "downloaded": info.path is not None,
            "state": info.state, "label": MODEL_STATES[info.state], "problem": problem.strip(),
            "importable": info.importable is not None,
            "path": str(info.path) if info.path else None,
            "size_mb": round(folder_size(info.path) / 1e6) if info.path else None}


def custom_folder_status(folder: str) -> tuple[str, str]:
    """State and message for the local model folder chosen in Settings."""
    state, problems = inspect_folder(Path(folder))
    if state == "installed":
        return state, f"Local model folder OK: {folder}"
    if state == "not_downloaded":
        return state, f"The local model folder {folder} does not exist."
    return state, " ".join(problems) or f"{folder} is not a complete faster-whisper (CTranslate2) model."


# -- import from the old cache without following links ----------------------------------------

def _resolve_cache_entry(entry: Path, cache_root: Path) -> Path:
    """The real file behind a snapshot entry, without letting Windows traverse the link.

    A symbolic link is read as text (``os.readlink`` does not follow it) and must point into the
    cache's own ``blobs`` folder; anything else is refused. A regular file is returned as is.
    """
    import os

    if not entry.is_symlink():  # lstat: inspects the entry itself
        return entry
    target = os.readlink(entry)
    if target.startswith("\\\\?\\"):
        target = target[4:]
    resolved = Path(os.path.normpath(target if os.path.isabs(target) else os.path.join(str(entry.parent), target)))
    blobs = Path(os.path.normpath(str(cache_root / "blobs")))
    if blobs not in resolved.parents:
        raise OSError(f"the link {entry.name} points outside the model cache ({target})")
    return resolved


def _plan_import(snapshot: Path) -> list[tuple[str, Path]] | None:
    """``[(file name, real file)]`` for a snapshot with all required files, or None."""
    cache_root = snapshot.parent.parent
    plan = []
    try:
        for entry in snapshot.iterdir():
            source = _resolve_cache_entry(entry, cache_root)
            if not source.is_file():
                continue
            plan.append((entry.name, source))
    except OSError:
        return None
    names = {n for n, _s in plan}
    if not set(REQUIRED_FILES) <= names or not ("tokenizer.json" in names or any(n.startswith("vocabulary.")
                                                                                 for n in names)):
        return None
    return plan


def _verify_blob(source: Path, copy: Path) -> str | None:
    """Blob files are named by their checksum: SHA-256 for large files, the git object id otherwise."""
    from ai_video_studio.core.download import git_blob_sha1, sha256_of

    blob = source.name.lower()
    if len(blob) == 64 and all(c in "0123456789abcdef" for c in blob):
        return None if sha256_of(copy) == blob else "SHA-256 checksum does not match"
    if len(blob) == 40 and all(c in "0123456789abcdef" for c in blob):
        return None if git_blob_sha1(copy) == blob else "checksum does not match"
    return None  # not a blob name (a plain copied file): nothing to compare with


def import_from_cache(name: str, snapshot: Path, progress: Callable[[float, str], None] | None = None,
                      check_cancelled: Callable[[], None] | None = None) -> Path:
    """Copy a cached model into ``whisper/<name>`` as plain files, verifying each checksum.

    The old cache is left untouched. Raises DownloadError when it cannot be used (the caller then
    downloads the model instead).
    """
    plan = _plan_import(snapshot)
    if plan is None:
        raise DownloadError(f"The cached '{name}' model in {snapshot} is incomplete or unreadable.")
    root = whisper_root()
    staging = root / f".{name}.import"
    remove_tree(staging)
    staging.mkdir(parents=True)
    total = sum(src.stat().st_size for _n, src in plan) or 1
    done = 0
    try:
        for filename, source in plan:
            if check_cancelled:
                check_cancelled()
            target = staging / filename
            shutil.copyfile(source, target)
            problem = _verify_blob(source, target)
            if problem:
                raise DownloadError(f"The cached file {filename} of '{name}' is damaged ({problem}).")
            done += source.stat().st_size
            if progress:
                progress(min(1.0, done / total), f"Copying Whisper '{name}' from the old cache ({filename})")
        (staging / "download.json").write_text(json.dumps({"repo": WHISPER_MODELS[name].repo,
                                                           "imported_from": str(snapshot),
                                                           "files": [n for n, _s in plan]}, indent=2))
        if inspect_folder(staging)[0] != "installed":
            raise DownloadError(f"The cached '{name}' model is incomplete.")
        target = root / name
        remove_tree(target)
        staging.replace(target)
    except OSError as exc:
        raise DownloadError(f"The cached '{name}' model could not be copied: {exc}") from exc
    finally:
        if staging.exists():
            remove_tree(staging)
    log.info("Whisper model imported from the old cache", extra={"model": name, "snapshot": str(snapshot)})
    return target


def remove_model(name: str) -> bool:
    """Delete a downloaded model (both the current and the legacy location; only on user request)."""
    ok = remove_tree(whisper_root() / name)
    legacy = legacy_cache(name)
    try:
        if legacy.exists():
            ok = remove_tree(legacy) and ok
    except OSError as exc:
        log.warning("Could not remove the old model cache", extra={"path": str(legacy), "error": str(exc)})
        ok = False
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
    info = inspect_model(name)
    if info.path is not None:
        return info.path
    if info.importable is not None:
        try:
            return import_from_cache(name, info.importable, progress, check_cancelled)
        except DownloadError as exc:
            log.warning("Import from the old cache failed; downloading instead", extra={"model": name,
                                                                                         "error": str(exc)})
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

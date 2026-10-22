"""Cache of analysis results for export jobs (no Qt): transcripts, Smart Cut audio levels and
face/speaker tracking.

An entry is found only for the **same file** (resolved path, size and modification time) and the
**same settings** (the ``config`` that produced it, e.g. Whisper model and language, or the
tracking rate, features and threshold). A changed or replaced source therefore never reuses an
old result, and results of another video are never used. Entries are JSON files in the
application's temp folder (never next to the videos); the oldest are removed beyond a size limit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path

from ai_video_studio.core import paths
from ai_video_studio.core.fs import remove_file

log = logging.getLogger(__name__)

VERSION = 1             # bump when the stored format of any kind changes
MAX_BYTES = 512 * 1024 ** 2
_lock = threading.Lock()


def cache_dir() -> Path:
    folder = paths.temp_dir() / "analysis-cache"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def source_identity(path: str | Path) -> dict | None:
    """What identifies the file's content cheaply; ``None`` if it cannot be read."""
    try:
        resolved = Path(path).resolve()
        stat = resolved.stat()
    except OSError:
        return None
    return {"path": os.path.normcase(str(resolved)), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _key(kind: str, source: dict, config: dict) -> str:
    text = json.dumps({"v": VERSION, "kind": kind, "source": source, "config": config}, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get(kind: str, path: str | Path, config: dict) -> dict | None:
    """The cached result for this file and settings, or ``None``."""
    source = source_identity(path)
    if source is None:
        return None
    entry = cache_dir() / f"{kind}-{_key(kind, source, config)}.json"
    try:
        data = json.loads(entry.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("source") != source or data.get("config") != config:  # never trust a mismatching entry
        return None
    try:
        os.utime(entry)  # recently used: kept longest
    except OSError:
        pass
    log.info("Analysis reused from the cache", extra={"kind": kind, "source": source["path"]})
    return data.get("result")


def put(kind: str, path: str | Path, config: dict, result: dict) -> None:
    source = source_identity(path)
    if source is None:
        return
    entry = cache_dir() / f"{kind}-{_key(kind, source, config)}.json"
    tmp = entry.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({"source": source, "config": config, "result": result}), encoding="utf-8")
        os.replace(tmp, entry)
    except OSError as exc:
        log.warning("Analysis could not be cached", extra={"kind": kind, "error": str(exc)})
        remove_file(tmp)
        return
    prune()


def prune(limit: int = MAX_BYTES) -> None:
    """Remove the least recently used entries above ``limit`` bytes."""
    with _lock:
        try:
            files = [(f.stat().st_mtime, f.stat().st_size, f) for f in cache_dir().glob("*.json")]
        except OSError:
            return
        total = sum(size for _m, size, _f in files)
        for _mtime, size, f in sorted(files):
            if total <= limit:
                break
            if remove_file(f):
                total -= size


def clear() -> int:
    removed = 0
    for f in cache_dir().glob("*.json"):
        removed += bool(remove_file(f))
    return removed

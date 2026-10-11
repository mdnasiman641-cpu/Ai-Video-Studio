"""File downloads with progress, retries, resume and checksum verification (stdlib only).

Used for AI models. A download goes to ``<target>.part`` first and is renamed only after its
checksum matched, so a cancelled or failed download never leaves a file that looks complete.
An interrupted ``.part`` file is resumed with an HTTP Range request when the server allows it.
Proxy settings come from the usual environment variables (``HTTPS_PROXY``).
"""

from __future__ import annotations

import hashlib
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from ai_video_studio import APP_NAME, __version__
from ai_video_studio.core.errors import JobCancelled, StudioError

log = logging.getLogger(__name__)

CHUNK = 1 << 20
USER_AGENT = f"{APP_NAME.replace(' ', '')}/{__version__}"

Progress = Callable[[int, int | None], None]   # (bytes done, total bytes or None)
Opener = Callable[[urllib.request.Request, float], object]


class DownloadError(StudioError):
    pass


def git_blob_sha1(path: Path) -> str:
    """Git's object id for a file (what Hugging Face reports for files not stored in LFS)."""
    digest = hashlib.sha1()
    digest.update(f"blob {path.stat().st_size}\0".encode())
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path: Path, size: int | None = None, sha256: str | None = None, git_sha1: str | None = None) -> str | None:
    """``None`` when ``path`` matches every given expectation, else a description of the mismatch."""
    if size is not None and path.stat().st_size != size:
        return f"size {path.stat().st_size} instead of {size}"
    if sha256 and sha256_of(path) != sha256.lower():
        return "SHA-256 checksum does not match"
    if git_sha1 and git_blob_sha1(path) != git_sha1.lower():
        return "checksum does not match"
    return None


def _default_opener(request: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - https URLs from a fixed list


def fetch_json(url: str, opener: Opener | None = None, timeout: float = 30, retries: int = 3,
               sleep: Callable[[float], None] | None = None) -> dict:
    import json

    sleep = sleep or time.sleep
    open_url = opener or _default_opener
    last: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with open_url(request, timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403, 404):
                break
            sleep(2 ** attempt)
    raise DownloadError(f"Could not reach {url.split('/')[2]}: {last}") from last


def download_file(
    url: str,
    target: Path,
    *,
    size: int | None = None,
    sha256: str | None = None,
    git_sha1: str | None = None,
    progress: Progress | None = None,
    check_cancelled: Callable[[], None] | None = None,
    retries: int = 4,
    timeout: float = 60,
    opener: Opener | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Path:
    """Download ``url`` to ``target`` and verify it. Existing verified files are kept."""
    sleep = sleep or time.sleep
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and verify(target, size, sha256, git_sha1) is None:
        if progress:
            progress(target.stat().st_size, target.stat().st_size)
        return target
    part = target.with_name(target.name + ".part")
    open_url = opener or _default_opener
    last: Exception | None = None
    for attempt in range(retries):
        if check_cancelled:
            check_cancelled()
        have = part.stat().st_size if part.exists() else 0
        if size is not None and have > size:
            part.unlink()
            have = 0
        headers = {"User-Agent": USER_AGENT}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            request = urllib.request.Request(url, headers=headers)
            with open_url(request, timeout) as response:
                status = getattr(response, "status", 200)
                if have and status != 206:  # server ignored the range: start over
                    have = 0
                length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
                total = size or (int(length) + have if length else None)
                with open(part, "ab" if have else "wb") as out:
                    done = have
                    while True:
                        if check_cancelled:
                            check_cancelled()
                        chunk = response.read(CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
            if total is not None and part.stat().st_size < total:
                # The connection ended early: keep what arrived and resume with a Range request.
                raise ConnectionError(f"connection closed after {part.stat().st_size} of {total} bytes")
            problem = verify(part, size, sha256, git_sha1)
            if problem is None:
                part.replace(target)
                return target
            part.unlink()  # corrupt: start again from zero
            last = DownloadError(f"{target.name}: {problem}")
            log.warning("Downloaded file failed verification", extra={"file": target.name, "problem": problem})
        except JobCancelled:
            raise
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (401, 403, 404):
                break
        except (urllib.error.URLError, OSError) as exc:
            last = exc
            log.warning("Download interrupted; retrying", extra={"file": target.name, "error": str(exc),
                                                                  "attempt": attempt + 1})
        sleep(min(30, 2 ** attempt))
    raise DownloadError(f"Download of {target.name} failed: {last}")

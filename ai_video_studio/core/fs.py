"""File-system helpers that cope with Windows file locking."""

from __future__ import annotations

import shutil
import time
from pathlib import Path


def remove_file(path: str | Path, attempts: int = 30, delay: float = 0.1) -> bool:
    """Delete a file, retrying briefly.

    On Windows a just-killed process (e.g. a cancelled FFmpeg export) can keep its output
    file locked for a short time, so a single unlink fails with PermissionError.
    Returns True if the file no longer exists.
    """
    path = Path(path)
    for _ in range(attempts):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(delay)
    return not path.exists()


def remove_tree(path: str | Path, attempts: int = 30, delay: float = 0.1) -> bool:
    """``shutil.rmtree`` with the same retry behaviour as :func:`remove_file`."""
    path = Path(path)
    for _ in range(attempts):
        if not path.exists():
            return True
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        time.sleep(delay)
    return not path.exists()

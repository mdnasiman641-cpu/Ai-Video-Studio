"""Application data locations.

All writable state (database, logs, temp files, downloaded models) lives in a per-user
data directory (``%LOCALAPPDATA%\\AIVideoStudio`` on Windows), never next to source videos
and never inside the installation folder, so upgrading or reinstalling keeps it.
``AVS_DATA_DIR`` overrides the location (tests). A packaged portable copy that has a
``portable.txt`` file next to its executable keeps its data in ``UserData`` beside it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ai_video_studio import APP_ID


PORTABLE_MARKER = "portable.txt"


def is_frozen() -> bool:
    """True when running as the packaged executable (PyInstaller)."""
    return bool(getattr(sys, "frozen", False))


def exe_dir() -> Path:
    """Folder of the packaged executable, or the source checkout when not packaged."""
    return Path(sys.executable).resolve().parent if is_frozen() else app_root()


def portable_data_dir() -> Path | None:
    if is_frozen() and (exe_dir() / PORTABLE_MARKER).is_file():
        return exe_dir() / "UserData"
    return None


def data_dir() -> Path:
    override = os.environ.get("AVS_DATA_DIR")
    portable = portable_data_dir()
    if override:
        base = Path(override)
    elif portable is not None:
        base = portable
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_ID
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_ID
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "ai-video-studio"
    base.mkdir(parents=True, exist_ok=True)
    return base


def logs_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def temp_dir() -> Path:
    path = data_dir() / "temp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    return data_dir() / "studio.db"


def default_projects_dir() -> Path:
    return Path.home() / "Documents" / "AI Video Studio Projects"


def models_dir() -> Path:
    """Downloaded AI models (kept across application upgrades)."""
    path = data_dir() / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def app_root() -> Path:
    """Directory containing the application's bundled resources (``_internal`` when frozen)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[2]

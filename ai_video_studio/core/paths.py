"""Application data locations.

All writable state (database, logs, temp files) lives in a per-user data
directory, never next to source videos. ``AVS_DATA_DIR`` overrides the location
(used by tests and portable installs).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ai_video_studio import APP_ID


def data_dir() -> Path:
    override = os.environ.get("AVS_DATA_DIR")
    if override:
        base = Path(override)
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


def app_root() -> Path:
    """Directory containing the application (the PyInstaller bundle dir when frozen)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[2]

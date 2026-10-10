"""Application services shared by the UI: database, settings, recent projects and jobs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ai_video_studio.core import paths
from ai_video_studio.core.ffmpeg_tools import find_tool, require_tool, tool_version
from ai_video_studio.jobs.handlers import PROBE_JOB, make_probe_handler
from ai_video_studio.jobs.manager import JobManager
from ai_video_studio.jobs.store import JobStore
from ai_video_studio.storage.database import Database
from ai_video_studio.storage.project import RecentProjects
from ai_video_studio.storage.settings import SettingsStore

log = logging.getLogger(__name__)


@dataclass
class ToolStatus:
    ffmpeg: Path | None
    ffprobe: Path | None
    ffmpeg_version: str | None
    ffprobe_version: str | None

    @property
    def ok(self) -> bool:
        return self.ffmpeg is not None and self.ffprobe is not None


class Services:
    def __init__(self, db_path: Path | str | None = None):
        self.db = Database(db_path or paths.database_path())
        self.settings = SettingsStore(self.db)
        self.recent = RecentProjects(self.db)
        self.jobs = JobManager(JobStore(self.db), self.settings.get("max_concurrent_jobs"))
        self.jobs.register(PROBE_JOB, make_probe_handler(self.ffprobe_path))
        self.settings.subscribe(self._on_setting_changed)

    def ffprobe_path(self) -> Path:
        return require_tool("ffprobe", self.settings.get("ffmpeg_dir"))

    def ffmpeg_path(self) -> Path:
        return require_tool("ffmpeg", self.settings.get("ffmpeg_dir"))

    def tool_status(self) -> ToolStatus:
        configured = self.settings.get("ffmpeg_dir")
        ffmpeg = find_tool("ffmpeg", configured)
        ffprobe = find_tool("ffprobe", configured)
        return ToolStatus(
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            ffmpeg_version=tool_version(ffmpeg) if ffmpeg else None,
            ffprobe_version=tool_version(ffprobe) if ffprobe else None,
        )

    def _on_setting_changed(self, key: str, value) -> None:
        if key == "max_concurrent_jobs":
            self.jobs.set_max_concurrent(value)
        elif key == "log_level":
            from ai_video_studio.core.logging_setup import set_level

            set_level(value)

    def close(self) -> None:
        self.jobs.shutdown()
        self.db.close()

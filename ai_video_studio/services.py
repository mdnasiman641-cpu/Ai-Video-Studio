"""Application services shared by the UI: database, settings, recent projects and jobs."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ai_video_studio.core import paths
from ai_video_studio.core.errors import StudioError
from ai_video_studio.core.ffmpeg_tools import find_tool, require_tool, tool_version
from ai_video_studio.jobs.caption_jobs import (
    BURN_IN_JOB,
    CORRECTION_JOB,
    FRAME_PREVIEW_JOB,
    MODEL_DOWNLOAD_JOB,
    TRANSCRIBE_JOB,
    make_burn_in_handler,
    make_correction_handler,
    make_frame_preview_handler,
    make_model_download_handler,
    make_transcribe_handler,
)
from ai_video_studio.ai.profiles import ProviderProfile, ProviderStore
from ai_video_studio.ai.router import AIRouter
from ai_video_studio.ai.usage import UsageLog
from ai_video_studio.jobs.ai_jobs import AI_TEST_JOB, AI_TITLES_JOB, make_ai_test_handler, make_ai_titles_handler
from ai_video_studio.jobs.handlers import PROBE_JOB, make_probe_handler
from ai_video_studio.jobs.manager import JobManager
from ai_video_studio.jobs.reframe_jobs import (
    REFRAME_ANALYZE_JOB,
    REFRAME_EXPORT_JOB,
    REFRAME_PREVIEW_JOB,
    make_reframe_analyze_handler,
    make_reframe_export_handler,
    make_reframe_preview_handler,
)
from ai_video_studio.jobs.render_jobs import (
    RENDER_CLIP_JOB,
    RENDER_FRAME_JOB,
    RENDER_JOB,
    SMARTCUT_ANALYZE_JOB,
    make_render_clip_handler,
    make_render_frame_handler,
    make_render_handler,
    make_smartcut_analyze_handler,
)
from ai_video_studio.jobs.store import JobStore
from ai_video_studio.render.batch import BatchStore, RenderPresetStore
from ai_video_studio.render.color import ColorPresetStore
from ai_video_studio.render.output import OutputSettings
from ai_video_studio.storage.database import Database
from ai_video_studio.storage import secrets
from ai_video_studio.storage.presets import PresetStore
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
        self.presets = PresetStore(self.db)
        self.render_presets = RenderPresetStore(self.db)
        self.color_presets = ColorPresetStore(self.settings)
        self.batches = BatchStore(self.db)
        self.ai_providers = ProviderStore(self.db)
        self.ai_providers.ensure_defaults()
        self.ai_usage = UsageLog(self.db)
        self.jobs = JobManager(JobStore(self.db), self.settings.get("max_concurrent_jobs"),
                               self.settings.get("max_concurrent_renders"))
        self.jobs.register(PROBE_JOB, make_probe_handler(self.ffprobe_path))
        self.jobs.register(TRANSCRIBE_JOB, make_transcribe_handler(self.ffmpeg_path))
        self.jobs.register(BURN_IN_JOB, make_burn_in_handler(self.ffmpeg_path), heavy=True)
        self.jobs.register(FRAME_PREVIEW_JOB, make_frame_preview_handler(self.ffmpeg_path))
        self.jobs.register(CORRECTION_JOB, make_correction_handler(secrets.get_api_key, usage_log=self.ai_usage))
        self.jobs.register(AI_TEST_JOB, make_ai_test_handler(secrets.get_api_key, usage_log=self.ai_usage))
        self.jobs.register(AI_TITLES_JOB, make_ai_titles_handler(secrets.get_api_key, usage_log=self.ai_usage))
        self.jobs.register(MODEL_DOWNLOAD_JOB, make_model_download_handler())
        self.jobs.register(REFRAME_ANALYZE_JOB, make_reframe_analyze_handler(self.ffmpeg_path))
        self.jobs.register(REFRAME_EXPORT_JOB, make_reframe_export_handler(self.ffmpeg_path), heavy=True)
        self.jobs.register(REFRAME_PREVIEW_JOB, make_reframe_preview_handler(self.ffmpeg_path))
        self.jobs.register(SMARTCUT_ANALYZE_JOB, make_smartcut_analyze_handler(self.ffmpeg_path))
        self.jobs.register(RENDER_JOB, make_render_handler(self.ffmpeg_path, get_ffprobe=self.ffprobe_path),
                           heavy=True)
        self.jobs.register(RENDER_FRAME_JOB, make_render_frame_handler(self.ffmpeg_path))
        self.jobs.register(RENDER_CLIP_JOB, make_render_clip_handler(self.ffmpeg_path))
        self.settings.subscribe(self._on_setting_changed)

    def ffprobe_path(self) -> Path:
        return require_tool("ffprobe", self.settings.get("ffmpeg_dir"))

    def ffmpeg_path(self) -> Path:
        return require_tool("ffmpeg", self.settings.get("ffmpeg_dir"))

    def output_defaults(self) -> OutputSettings:
        """Export settings for edited exports: the last ones used, else based on Settings → Export."""
        raw = self.settings.get("export_output_settings")
        if raw:
            try:
                settings = OutputSettings.from_dict(json.loads(raw))
                settings.validate()
                return settings
            except (ValueError, TypeError, StudioError):
                pass
        encoder = {"h264_nvenc": "gpu", "libx264": "cpu"}.get(self.settings.get("video_encoder"), "auto")
        return OutputSettings(encoder=encoder, quality="custom", crf=self.settings.get("video_quality"))

    def remember_output_settings(self, settings: OutputSettings) -> None:
        settings.validate()
        self.settings.set("export_output_settings", json.dumps(settings.to_dict()))

    def ai_profile_key(self, profile: ProviderProfile) -> str | None:
        return secrets.profile_key(profile.id, profile.key_env)

    def ai_plan(self, capability: str) -> tuple[list[ProviderProfile], list[str]]:
        """Profiles that would be tried for ``capability`` (in order) and why others are not used."""
        router = AIRouter(self.ai_providers.list(), self.ai_profile_key)
        usable, skipped = router.plan(capability)
        return [p for p, _key in usable], skipped

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
        elif key == "max_concurrent_renders":
            self.jobs.set_max_heavy(value)
        elif key == "log_level":
            from ai_video_studio.core.logging_setup import set_level

            set_level(value)

    def close(self) -> bool:
        """Returns False if a job was still running (its worker thread cannot be stopped)."""
        stopped = self.jobs.shutdown()
        # Release a loaded Whisper model now rather than during interpreter teardown, when
        # native library destructors (CTranslate2/OpenMP) may run in an unsafe order.
        from ai_video_studio.captions.transcriber import clear_model_cache

        clear_model_cache()
        if not stopped:
            log.warning("A background job did not stop in time; it will be marked interrupted on next start")
            return False
        self.db.close()
        return True

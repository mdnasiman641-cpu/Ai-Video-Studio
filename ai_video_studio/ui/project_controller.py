"""Holds the open project and connects it to background jobs.

All methods run on the GUI thread. Job updates arrive through :class:`JobBridge`,
which re-emits listener calls from worker threads as queued Qt signals.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ai_video_studio.captions.ass import build_ass
from ai_video_studio.captions.model import CaptionTrack
from ai_video_studio.captions.style import CaptionStyle
from ai_video_studio.core import paths
from ai_video_studio.core.errors import StudioError
from ai_video_studio.core.ffmpeg_tools import VideoMetadata
from ai_video_studio.core.media import MediaItem, MediaStatus, VIDEO_EXTENSIONS, validate_video_path
from ai_video_studio.jobs.caption_jobs import BURN_IN_JOB, CORRECTION_JOB, FRAME_PREVIEW_JOB, TRANSCRIBE_JOB
from ai_video_studio.jobs.handlers import PROBE_JOB
from ai_video_studio.jobs.models import Job, JobState
from ai_video_studio.services import Services
from ai_video_studio.storage.project import Project

log = logging.getLogger(__name__)


class JobBridge(QObject):
    job_changed = Signal(object)

    def __init__(self, services: Services):
        super().__init__()
        services.jobs.add_listener(self.job_changed.emit)


class ImportReport:
    def __init__(self) -> None:
        self.added: list[str] = []
        self.duplicates: list[str] = []
        self.rejected: list[tuple[str, str]] = []

    def summary(self) -> str:
        parts = [f"{len(self.added)} video(s) imported"]
        if self.duplicates:
            parts.append(f"{len(self.duplicates)} already in project")
        if self.rejected:
            parts.append(f"{len(self.rejected)} skipped")
        return ", ".join(parts)


def expand_paths(paths: list[str]) -> list[str]:
    """Expand folders (recursively) to the video files they contain."""
    result: list[str] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            result += sorted(str(f) for f in p.rglob("*") if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS)
        else:
            result.append(str(p))
    return result


class ProjectController(QObject):
    project_changed = Signal()          # a different project was opened/created/closed
    media_list_changed = Signal()       # items added/removed
    media_updated = Signal(str)         # one item's metadata/status changed
    dirty_changed = Signal(bool)
    captions_changed = Signal(str)      # media id: caption track or style changed
    transcription_finished = Signal(str, object)  # media id, job (completed or failed)
    corrections_ready = Signal(str)     # media id: AI suggestions are waiting for review

    def __init__(self, services: Services, bridge: JobBridge):
        super().__init__()
        self.services = services
        self.project: Project | None = None
        bridge.job_changed.connect(self._on_job_changed)

    # -- project lifecycle ---------------------------------------------------
    def new_project(self, name: str) -> Project:
        self._set_project(Project(name=name.strip() or "Untitled"))
        log.info("New project created", extra={"project_name": self.project.name})
        return self.project

    def open_project(self, path: str | Path) -> Project:
        project = Project.load(path)  # raises ProjectError
        self._set_project(project)
        self.services.recent.touch(project)
        self.probe_pending()
        self._recover_transcriptions()
        return project

    def save_project(self, path: str | Path | None = None) -> Path:
        assert self.project is not None
        saved = self.project.save(Path(path) if path else None)
        self.services.recent.touch(self.project)
        self.dirty_changed.emit(False)
        return saved

    def close_project(self) -> None:
        self._set_project(None)

    def ensure_project(self) -> Project:
        if self.project is None:
            self.new_project("Untitled")
        return self.project

    def _set_project(self, project: Project | None) -> None:
        self.project = project
        self.project_changed.emit()
        self.media_list_changed.emit()
        self.dirty_changed.emit(bool(project and project.dirty))

    # -- media ---------------------------------------------------------------
    def import_paths(self, paths: list[str]) -> ImportReport:
        project = self.ensure_project()
        report = ImportReport()
        for path in expand_paths(paths):
            error = validate_video_path(path)
            if error:
                report.rejected.append((path, error))
                continue
            item = project.add_media(path)
            if item is None:
                report.duplicates.append(path)
                continue
            report.added.append(item.id)
            self._queue_probe(item.id)
        if report.added:
            self.media_list_changed.emit()
            self.dirty_changed.emit(True)
        log.info("Media import", extra={"added": len(report.added), "duplicates": len(report.duplicates),
                                        "rejected": len(report.rejected)})
        return report

    def remove_media(self, media_ids: set[str]) -> int:
        if self.project is None:
            return 0
        removed = self.project.remove_media(media_ids)
        if removed:
            self.media_list_changed.emit()
            self.dirty_changed.emit(True)
        return removed

    def reprobe(self, media_ids: set[str]) -> int:
        if self.project is None:
            return 0
        count = 0
        for media_id in media_ids:
            item = self.project.find_media(media_id)
            if item is None or item.status is MediaStatus.MISSING:
                continue
            item.error = None
            item.metadata = None
            self.media_updated.emit(media_id)
            self._queue_probe(media_id)
            count += 1
        return count

    def probe_pending(self) -> None:
        if self.project is None:
            return
        for item in self.project.media:
            if item.status is MediaStatus.PENDING:
                self._queue_probe(item.id)

    def _queue_probe(self, media_id: str) -> None:
        item = self.project.find_media(media_id)
        self.services.jobs.submit(
            PROBE_JOB,
            f"Read metadata: {item.name}",
            {"path": item.path, "media_id": media_id},
            project_id=self.project.id,
        )

    # -- captions --------------------------------------------------------------
    def media(self, media_id: str) -> MediaItem:
        item = self.project.find_media(media_id) if self.project else None
        if item is None:
            raise StudioError("The video is no longer in the project.")
        return item

    def set_captions(self, media_id: str, track: CaptionTrack | None) -> None:
        self.media(media_id).captions = track
        self._caption_edit(media_id)

    def set_caption_style(self, media_ids: list[str] | set[str], style: CaptionStyle) -> None:
        style.validate()
        for media_id in media_ids:
            self.media(media_id).caption_style = CaptionStyle.from_dict(style.to_dict())
            self._caption_edit(media_id)

    def _caption_edit(self, media_id: str) -> None:
        self.project.dirty = True
        self.captions_changed.emit(media_id)
        self.dirty_changed.emit(True)

    def active_jobs(self, media_id: str, kind: str) -> list[Job]:
        if self.project is None:
            return []
        jobs = self.services.jobs.store.list({JobState.QUEUED, JobState.RUNNING})
        return [j for j in jobs if j.kind == kind and j.project_id == self.project.id
                and j.payload.get("media_id") == media_id]

    def submit_transcription(self, media_ids: list[str], language: str, model: str, device: str) -> list[Job]:
        settings = self.services.settings
        jobs = []
        for media_id in media_ids:
            item = self.media(media_id)
            if item.status is MediaStatus.MISSING:
                raise StudioError(f"{item.name}: the source file is missing.")
            if self.active_jobs(media_id, TRANSCRIBE_JOB):
                continue
            payload = {
                "path": item.path,
                "media_id": media_id,
                "language": language,
                "model": model,
                "device": device,
                "model_path": settings.get("whisper_model_dir") or None,
                "models_dir": str(paths.data_dir() / "models"),
                "max_chars": settings.get("caption_max_chars"),
                "max_seconds": settings.get("caption_max_seconds"),
                "duration": item.metadata.duration if item.metadata else None,
            }
            jobs.append(self.services.jobs.submit(TRANSCRIBE_JOB, f"Captions: {item.name}", payload,
                                                  project_id=self.project.id))
        return jobs

    def caption_ass(self, media_id: str) -> str:
        item = self.media(media_id)
        if item.metadata is None:
            raise StudioError("Video information has not been read yet.")
        if not item.captions or not item.captions.segments:
            raise StudioError("This video has no captions yet.")
        meta = item.metadata
        return build_ass(item.captions.segments, item.effective_caption_style,
                         meta.display_width, meta.display_height, title=item.name)

    def submit_burn_in(self, media_id: str, output: str) -> Job:
        item = self.media(media_id)
        if Path(output).resolve() == Path(item.path).resolve():
            raise StudioError("Choose a different file name: the source video is never overwritten.")
        meta = item.metadata
        payload = {
            "path": item.path, "media_id": media_id, "output": output,
            "ass": self.caption_ass(media_id),
            "duration": meta.duration, "audio_codec": meta.audio_codec, "has_audio": meta.has_audio,
            "encoder": self.services.settings.get("video_encoder"),
            "quality": self.services.settings.get("video_quality"),
        }
        return self.services.jobs.submit(BURN_IN_JOB, f"Export with captions: {item.name}", payload,
                                         project_id=self.project.id)

    def submit_frame_preview(self, media_id: str, at_seconds: float) -> Job:
        item = self.media(media_id)
        payload = {"path": item.path, "media_id": media_id, "ass": self.caption_ass(media_id), "at": at_seconds}
        return self.services.jobs.submit(FRAME_PREVIEW_JOB, f"Caption frame preview: {item.name}", payload,
                                         project_id=self.project.id)

    def submit_correction(self, media_id: str) -> Job:
        config = self.services.ai_correction_config()
        if config is None:
            raise StudioError("AI correction is off. Choose a provider and add its API key in Settings.")
        item = self.media(media_id)
        if not item.captions or not item.captions.segments:
            raise StudioError("This video has no captions to check.")
        provider, model = config
        payload = {
            "media_id": media_id, "provider": provider, "model": model,
            "language": item.captions.language,
            "timeout": self.services.settings.get("ai_timeout_seconds"),
            "segments": [{"id": s.id, "text": s.text} for s in item.captions.segments],
        }
        return self.services.jobs.submit(CORRECTION_JOB, f"AI caption check: {item.name}", payload,
                                         project_id=self.project.id)

    def _recover_transcriptions(self) -> None:
        """Apply transcriptions that finished while their project was closed (only where no captions exist)."""
        completed = self.services.jobs.store.list({JobState.COMPLETED})
        for job in completed:
            if job.kind != TRANSCRIBE_JOB or job.project_id != self.project.id or not job.result:
                continue
            item = self.project.find_media(job.result.get("media_id", ""))
            if item is not None and item.captions is None:
                item.captions = CaptionTrack.from_dict(job.result["track"])
                self.project.dirty = True
                log.info("Recovered transcription result", extra={"job_id": job.id, "media_id": item.id})
        if self.project.dirty:
            self.dirty_changed.emit(True)

    def _on_job_changed(self, job: Job) -> None:
        if self.project is None or job.project_id != self.project.id:
            return
        if job.kind == TRANSCRIBE_JOB:
            self._on_transcription(job)
            return
        if job.kind == CORRECTION_JOB:
            self._on_correction(job)
            return
        if job.kind != PROBE_JOB:
            return
        item = self.project.find_media(job.payload.get("media_id", ""))
        if item is None:
            return
        if job.state is JobState.COMPLETED and job.result:
            item.metadata = VideoMetadata.from_dict(job.result["metadata"])
            item.error = None
        elif job.state is JobState.FAILED:
            item.metadata = None
            item.error = job.error or "Could not read file"
        else:
            return
        self.project.dirty = True
        self.media_updated.emit(item.id)
        self.dirty_changed.emit(True)

    def _on_transcription(self, job: Job) -> None:
        media_id = job.payload.get("media_id", "")
        item = self.project.find_media(media_id)
        if item is None or not job.state.is_finished:
            return
        if job.state is JobState.COMPLETED and job.result:
            item.captions = CaptionTrack.from_dict(job.result["track"])
            self._caption_edit(media_id)
        self.transcription_finished.emit(media_id, job)

    def _on_correction(self, job: Job) -> None:
        media_id = job.payload.get("media_id", "")
        item = self.project.find_media(media_id)
        if item is None or job.state is not JobState.COMPLETED or not job.result or item.captions is None:
            return
        item.captions.pending_corrections = list(job.result.get("suggestions", []))
        self.project.dirty = True
        self.dirty_changed.emit(True)
        self.corrections_ready.emit(media_id)

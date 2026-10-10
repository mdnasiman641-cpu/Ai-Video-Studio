"""Holds the open project and connects it to background jobs.

All methods run on the GUI thread. Job updates arrive through :class:`JobBridge`,
which re-emits listener calls from worker threads as queued Qt signals.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ai_video_studio.core.ffmpeg_tools import VideoMetadata
from ai_video_studio.core.media import MediaStatus, VIDEO_EXTENSIONS, validate_video_path
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

    def _on_job_changed(self, job: Job) -> None:
        if job.kind != PROBE_JOB or self.project is None or job.project_id != self.project.id:
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

"""Holds the open project and connects it to background jobs.

All methods run on the GUI thread. Job updates arrive through :class:`JobBridge`,
which re-emits listener calls from worker threads as queued Qt signals.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ai_video_studio.captions.ass import build_ass
from ai_video_studio.captions.model import CaptionSegment, CaptionTrack, output_captions, remap_to_cuts
from ai_video_studio.captions.style import CaptionStyle
from ai_video_studio.core import paths
from ai_video_studio.core.errors import StudioError
from ai_video_studio.core.ffmpeg_tools import VideoMetadata
from ai_video_studio.core.media import MediaItem, MediaStatus, VIDEO_EXTENSIONS, validate_video_path
from ai_video_studio.captions.models import WHISPER_MODELS, installed_path
from ai_video_studio.jobs.caption_jobs import (
    BURN_IN_JOB,
    CORRECTION_JOB,
    FRAME_PREVIEW_JOB,
    MODEL_DOWNLOAD_JOB,
    TRANSCRIBE_JOB,
)
from ai_video_studio.jobs.ai_jobs import AI_TITLES_JOB
from ai_video_studio.jobs.handlers import PROBE_JOB
from ai_video_studio.jobs.models import Job, JobState
from ai_video_studio.jobs.reframe_jobs import REFRAME_ANALYZE_JOB, REFRAME_EXPORT_JOB, REFRAME_PREVIEW_JOB
from ai_video_studio.jobs.render_jobs import RENDER_FRAME_JOB, RENDER_JOB, SMARTCUT_ANALYZE_JOB, output_key
from ai_video_studio.render.effects import EditEffects
from ai_video_studio.render.layout import Layout, LayoutError, ResizeSettings, compute_layout, layout_notes
from ai_video_studio.render.output import OutputSettings
from ai_video_studio.smartcut.levels import AudioLevels
from ai_video_studio.smartcut.model import SmartCutState
from ai_video_studio.reframe.framing import (
    ASPECT_PRESETS,
    CropPath,
    caption_band_from_style,
    compute_crop_path,
    resolve_output_size,
)
from ai_video_studio.reframe.model import ReframeSettings, ReframeState, TrackingAnalysis, reanalysis_reasons
from ai_video_studio.services import Services
from ai_video_studio.storage.project import Project
from ai_video_studio.storage.settings import REFRAME_DEFAULT_KEYS

log = logging.getLogger(__name__)


class JobBridge(QObject):
    job_changed = Signal(object)

    def __init__(self, services: Services, parent: QObject | None = None):
        super().__init__(parent)
        emit = self.job_changed.emit
        services.jobs.add_listener(emit)
        # Stop forwarding when this object is destroyed (jobs may still finish afterwards).
        self.destroyed.connect(lambda *_: services.jobs.remove_listener(emit))


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
    reframe_changed = Signal(str)       # media id: reframe settings or tracking results changed
    reframe_analysis_finished = Signal(str, object)  # media id, job (completed, failed or cancelled)
    smartcut_changed = Signal(str)      # media id: cut proposal, settings or applied cuts changed
    smartcut_analysis_finished = Signal(str, object)  # media id, job
    resize_changed = Signal(str)        # media id: Resize & Crop settings changed
    effects_changed = Signal(str)       # media id: speed and audio effects changed

    def __init__(self, services: Services, bridge: JobBridge, parent: QObject | None = None):
        super().__init__(parent)
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
        self._recover_reframe_analyses()
        self._recover_smartcut_analyses()
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

    def model_to_download(self, model: str) -> str | None:
        """The Whisper model that would have to be downloaded first, or None when it is available."""
        if self.services.settings.get("whisper_model_dir"):
            return None  # a local model folder chosen in Settings
        return None if installed_path(model) is not None else model

    def submit_model_download(self, model: str) -> Job:
        active = [j for j in self.services.jobs.store.list({JobState.QUEUED, JobState.RUNNING})
                  if j.kind == MODEL_DOWNLOAD_JOB and j.payload.get("model") == model]
        if active:
            return active[0]
        spec = WHISPER_MODELS[model]
        return self.services.jobs.submit(MODEL_DOWNLOAD_JOB, f"Download Whisper model: {model} (~{spec.approx_mb} MB)",
                                         {"model": model})

    def submit_transcription(self, media_ids: list[str], language: str, model: str, device: str,
                             allow_download: bool = False) -> list[Job]:
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
                "allow_download": allow_download,
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

    def ai_providers_for(self, capability: str) -> list:
        """Profiles that would be tried (primary first); raises with the reasons when there are none."""
        profiles, skipped = self.services.ai_plan(capability)
        if not profiles:
            reasons = "; ".join(skipped) or "no providers are configured"
            raise StudioError(f"No AI provider is ready for this ({reasons}). Set one up on the AI Models page.")
        return profiles

    def submit_correction(self, media_id: str) -> Job:
        item = self.media(media_id)
        if not item.captions or not item.captions.segments:
            raise StudioError("This video has no captions to check.")
        profiles = self.ai_providers_for("caption_correction")
        payload = {
            "media_id": media_id, "capability": "caption_correction",
            "profiles": [p.to_dict() for p in profiles],  # snapshot without keys; keys are read when it runs
            "language": item.captions.language,
            "segments": [{"id": s.id, "text": s.text} for s in item.captions.segments],
        }
        return self.services.jobs.submit(CORRECTION_JOB, f"AI caption check: {item.name}", payload,
                                         project_id=self.project.id)

    def submit_titles(self, media_id: str) -> Job:
        item = self.media(media_id)
        if not item.captions or not item.captions.segments:
            raise StudioError("This video has no captions to base a title on.")
        profiles = self.ai_providers_for("titles")
        payload = {"media_id": media_id, "profiles": [p.to_dict() for p in profiles],
                   "language": item.captions.language,
                   "text": " ".join(s.text for s in item.captions.segments)}
        return self.services.jobs.submit(AI_TITLES_JOB, f"AI title and description: {item.name}", payload,
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
        if job.kind == REFRAME_ANALYZE_JOB:
            self._on_reframe_analysis(job)
            return
        if job.kind == SMARTCUT_ANALYZE_JOB:
            self._on_smartcut_analysis(job)
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

    # -- auto reframe ------------------------------------------------------------
    def default_reframe_settings(self) -> ReframeSettings:
        """Settings for a video that has not been reframed yet: the defaults from Settings → Tracking."""
        settings = ReframeSettings()
        for key, name in REFRAME_DEFAULT_KEYS.items():
            setattr(settings, name, self.services.settings.get(key))
        try:
            settings.validate()
        except StudioError:
            return ReframeSettings()
        return settings

    def reframe_state(self, media_id: str) -> ReframeState:
        """The video's reframe data (default settings if it has never been reframed)."""
        item = self.media(media_id)
        return item.reframe if item.reframe is not None else ReframeState(self.default_reframe_settings())

    def set_reframe_settings(self, media_id: str, settings: ReframeSettings) -> None:
        settings.validate()
        item = self.media(media_id)
        analysis = item.reframe.analysis if item.reframe else None
        item.reframe = ReframeState(ReframeSettings.from_dict(settings.to_dict()), analysis)
        self.project.dirty = True
        self.reframe_changed.emit(media_id)
        self.dirty_changed.emit(True)

    @staticmethod
    def analysis_is_stale(item: MediaItem) -> bool:
        """True if the source file changed since it was analysed."""
        analysis = item.reframe.analysis if item.reframe else None
        if analysis is None or analysis.source_size is None:
            return False
        try:
            stat = Path(item.path).stat()
        except OSError:
            return False
        return stat.st_size != analysis.source_size or abs(round(stat.st_mtime, 3) - (analysis.source_mtime or 0)) > 1

    def submit_reframe_analysis(self, media_id: str) -> Job:
        item = self.media(media_id)
        if item.status is MediaStatus.MISSING:
            raise StudioError(f"{item.name}: the source file is missing.")
        if item.metadata is None:
            raise StudioError("Video information has not been read yet.")
        active = self.active_jobs(media_id, REFRAME_ANALYZE_JOB)
        if active:
            return active[0]
        settings = self.reframe_state(media_id).settings
        meta = item.metadata
        if settings.tracking_mode == "center":
            raise StudioError("Center Crop does not use an analysis. Choose a tracking mode first.")
        features = settings.analysis_features()
        if "speech" in features and not meta.has_audio:
            raise StudioError(f"{settings.mode_info.label} needs speech from the audio, but this video has no "
                              f"audio track. Use Auto Face Tracking or Manual Subject Tracking.")
        payload = {
            "path": item.path, "media_id": media_id,
            "width": meta.display_width, "height": meta.display_height, "duration": meta.duration,
            "sample_fps": settings.analysis_rate(), "detect_persons": "persons" in features,
            "features": features, "face_threshold": settings.confidence_threshold,
            "models_dir": str(paths.data_dir() / "models"),
        }
        return self.services.jobs.submit(REFRAME_ANALYZE_JOB, f"Analyse faces/people: {item.name}", payload,
                                         project_id=self.project.id)

    def reframe_crop_path(self, media_id: str) -> CropPath:
        """The crop path for the current settings (raises ReframeError when it cannot be computed)."""
        item = self.media(media_id)
        if item.metadata is None:
            raise StudioError("Video information has not been read yet.")
        state = self.reframe_state(media_id)
        meta = item.metadata
        band = None
        if item.captions and item.captions.segments:
            band = caption_band_from_style(item.effective_caption_style)
        return compute_crop_path(state.analysis, state.settings, meta.display_width, meta.display_height,
                                 meta.duration, caption_band=band)

    def reframe_output_size(self, media_id: str) -> tuple[int, int]:
        meta = self.media(media_id).metadata
        return resolve_output_size(self.reframe_state(media_id).settings, meta.display_width, meta.display_height)

    def reframe_ass(self, media_id: str) -> str | None:
        """Caption ASS for the reframed output size, or None when captions are not burned in."""
        item = self.media(media_id)
        if not self.reframe_state(media_id).settings.burn_captions or not item.captions or not item.captions.segments:
            return None
        out_w, out_h = self.reframe_output_size(media_id)
        return build_ass(item.captions.segments, item.effective_caption_style, out_w, out_h, title=item.name)

    def _reframe_payload(self, media_id: str, path: CropPath) -> dict:
        item = self.media(media_id)
        meta = item.metadata
        out_w, out_h = self.reframe_output_size(media_id)
        return {
            "path": item.path, "media_id": media_id,
            "src_w": path.src_w, "src_h": path.src_h, "crop_w": path.crop_w, "crop_h": path.crop_h,
            "keyframes": path.keyframes(), "out_w": out_w, "out_h": out_h,
            "fps": meta.fps, "duration": meta.duration, "ass": self.reframe_ass(media_id),
        }

    def submit_reframe_export(self, media_id: str, output: str, path: CropPath | None = None) -> Job:
        item = self.media(media_id)
        if item.status is MediaStatus.MISSING:
            raise StudioError(f"{item.name}: the source file is missing.")
        if Path(output).resolve() == Path(item.path).resolve():
            raise StudioError("Choose a different file name: the source video is never overwritten.")
        path = path or self.reframe_crop_path(media_id)
        state = self.reframe_state(media_id)
        if state.settings.tracking_mode in ("manual", "auto_face", "auto_visible") and path.tracked_fraction == 0:
            raise StudioError("The selected subject is never visible, so the export would not follow anyone. "
                              "Choose another subject or Center crop.")
        meta = item.metadata
        payload = self._reframe_payload(media_id, path) | {
            "output": output, "audio_codec": meta.audio_codec, "has_audio": meta.has_audio,
            "encoder": self.services.settings.get("video_encoder"),
            "quality": self.services.settings.get("video_quality"),
        }
        return self.services.jobs.submit(REFRAME_EXPORT_JOB, f"Export reframed: {item.name}", payload,
                                         project_id=self.project.id)

    def submit_reframe_preview(self, media_id: str, at_seconds: float) -> Job:
        path = self.reframe_crop_path(media_id)
        payload = self._reframe_payload(media_id, path) | {"at": at_seconds}
        return self.services.jobs.submit(REFRAME_PREVIEW_JOB, f"Reframe frame preview: {self.media(media_id).name}",
                                         payload, project_id=self.project.id)

    def _apply_analysis(self, item: MediaItem, data: dict) -> list[str]:
        """Store a finished analysis; returns notes about settings that had to be reset."""
        analysis = TrackingAnalysis.from_dict(data)
        state = item.reframe or ReframeState()
        notes = []
        ids = {t.id for t in analysis.tracks}
        settings = state.settings
        if settings.target is not None and settings.target not in ids:
            settings.target = None
            notes.append("The previously selected subject no longer exists; automatic selection is used.")
        if any(int(tid) not in ids for _t, tid in settings.switches if tid is not None):
            settings.switches = []
            notes.append("Subject changes referred to old tracking results and were cleared.")
        item.reframe = ReframeState(settings, analysis)
        self.project.dirty = True
        return notes

    def _on_reframe_analysis(self, job: Job) -> None:
        media_id = job.payload.get("media_id", "")
        item = self.project.find_media(media_id)
        if item is None or not job.state.is_finished:
            return
        if job.state is JobState.COMPLETED and job.result and job.result.get("analysis"):
            notes = self._apply_analysis(item, job.result["analysis"])
            if notes:
                job.result.setdefault("notes", []).extend(notes)
            self.reframe_changed.emit(media_id)
            self.dirty_changed.emit(True)
        self.reframe_analysis_finished.emit(media_id, job)

    def _recover_reframe_analyses(self) -> None:
        """Apply analyses that finished while their project was closed (only where none is stored)."""
        completed = self.services.jobs.store.list({JobState.COMPLETED})
        for job in sorted(completed, key=lambda j: j.finished_at or "", reverse=True):  # newest first
            if job.kind != REFRAME_ANALYZE_JOB or job.project_id != self.project.id or not job.result:
                continue
            item = self.project.find_media(job.result.get("media_id", ""))
            if item is not None and (item.reframe is None or item.reframe.analysis is None):
                self._apply_analysis(item, job.result["analysis"])
                log.info("Recovered tracking analysis", extra={"job_id": job.id, "media_id": item.id})
        if self.project.dirty:
            self.dirty_changed.emit(True)

    # -- smart cut ---------------------------------------------------------------------
    def smartcut_state(self, media_id: str) -> SmartCutState:
        item = self.media(media_id)
        return item.smart_cut if item.smart_cut is not None else SmartCutState()

    def set_smartcut_state(self, media_id: str, state: SmartCutState) -> None:
        state.settings.validate()
        self.media(media_id).smart_cut = state
        self.project.dirty = True
        self.smartcut_changed.emit(media_id)
        self.dirty_changed.emit(True)

    def clear_smartcut(self, media_id: str) -> None:
        """Forget the audio analysis, proposal and applied cuts (the video file is not affected)."""
        self.media(media_id).smart_cut = None
        self.project.dirty = True
        self.smartcut_changed.emit(media_id)
        self.dirty_changed.emit(True)

    @staticmethod
    def smartcut_is_stale(item: MediaItem) -> bool:
        """True if the source file changed since its audio was analysed."""
        levels = item.smart_cut.levels if item.smart_cut else None
        if levels is None or levels.source_size is None:
            return False
        try:
            stat = Path(item.path).stat()
        except OSError:
            return False
        return stat.st_size != levels.source_size or abs(round(stat.st_mtime, 3) - (levels.source_mtime or 0)) > 1

    def submit_smartcut_analysis(self, media_id: str) -> Job:
        item = self.media(media_id)
        if item.status is MediaStatus.MISSING:
            raise StudioError(f"{item.name}: the source file is missing.")
        if item.metadata is None:
            raise StudioError("Video information has not been read yet.")
        if not item.metadata.has_audio:
            raise StudioError("This video has no audio track, so Smart Cut cannot detect silence in it.")
        active = self.active_jobs(media_id, SMARTCUT_ANALYZE_JOB)
        if active:
            return active[0]
        payload = {"path": item.path, "media_id": media_id, "duration": item.metadata.duration,
                   "has_audio": item.metadata.has_audio}
        return self.services.jobs.submit(SMARTCUT_ANALYZE_JOB, f"Analyse audio levels: {item.name}", payload,
                                         project_id=self.project.id)

    def _apply_levels(self, item: MediaItem, data: dict) -> None:
        state = item.smart_cut or SmartCutState()
        state.levels = AudioLevels.from_dict(data)
        state.detect()
        item.smart_cut = state
        self.project.dirty = True

    def _on_smartcut_analysis(self, job: Job) -> None:
        media_id = job.payload.get("media_id", "")
        item = self.project.find_media(media_id)
        if item is None or not job.state.is_finished:
            return
        if job.state is JobState.COMPLETED and job.result and job.result.get("levels"):
            self._apply_levels(item, job.result["levels"])
            self.smartcut_changed.emit(media_id)
            self.dirty_changed.emit(True)
        self.smartcut_analysis_finished.emit(media_id, job)

    def _recover_smartcut_analyses(self) -> None:
        """Apply audio analyses that finished while the project was closed (only where none is stored)."""
        for job in self.services.jobs.store.list({JobState.COMPLETED}):
            if job.kind != SMARTCUT_ANALYZE_JOB or job.project_id != self.project.id or not job.result:
                continue
            item = self.project.find_media(job.result.get("media_id", ""))
            if item is not None and (item.smart_cut is None or item.smart_cut.levels is None):
                self._apply_levels(item, job.result["levels"])
        if self.project.dirty:
            self.dirty_changed.emit(True)

    def applied_cuts(self, media_id: str) -> list[tuple[float, float]] | None:
        """The applied Smart Cut keep ranges when they cut something, else None."""
        item = self.media(media_id)
        return item.smart_cut.cut_ranges() if item.smart_cut else None

    def edited_captions(self, media_id: str, keep: list[tuple[float, float]] | None) -> list[CaptionSegment]:
        """The video's captions on the edited timeline (the caption track itself is not changed)."""
        item = self.media(media_id)
        if not item.captions or not item.captions.segments:
            return []
        segments = item.captions.segments
        return remap_to_cuts(segments, keep) if keep else [CaptionSegment.from_dict(s.to_dict()) for s in segments]

    def active_outputs(self) -> dict[str, Job]:
        """Output files that queued or running exports will write."""
        jobs = self.services.jobs.store.list({JobState.QUEUED, JobState.RUNNING})
        return {output_key(j.payload["output"]): j for j in jobs if j.payload.get("output")}

    def check_output(self, source: str, output: str, overwrite: bool) -> None:
        if output_key(output) == output_key(source):
            raise StudioError("Choose a different file name: the source video is never overwritten.")
        if output_key(output) in self.active_outputs():
            raise StudioError(f"{Path(output).name} is already being written by another export in the queue.")
        if Path(output).exists() and not overwrite:
            raise StudioError(f"{Path(output).name} already exists.")

    # -- speed and audio effects -------------------------------------------------------
    def effects(self, media_id: str) -> EditEffects:
        item = self.media(media_id)
        return item.effects.copy() if item.effects else EditEffects()

    def set_effects(self, media_id: str, effects: EditEffects) -> None:
        effects.validate()
        self.media(media_id).effects = effects.copy()
        self.project.dirty = True
        self.effects_changed.emit(media_id)
        self.dirty_changed.emit(True)

    def submit_render(self, media_id: str, output: str, settings: OutputSettings, *, use_cuts: bool = True,
                      burn_captions: bool = False, stage: dict | None = None, canvas: tuple[int, int] | None = None,
                      overwrite: bool = False, title: str | None = None, preview: bool = False,
                      use_effects: bool = True) -> Job:
        """Queue an edited export. ``stage``/``canvas`` describe resizing (None = source size).

        The video's speed and audio effects are included when they are switched on (``use_effects``).
        """
        item = self.media(media_id)
        if item.status is MediaStatus.MISSING:
            raise StudioError(f"{item.name}: the source file is missing.")
        if item.metadata is None:
            raise StudioError("Video information has not been read yet.")
        settings.validate()
        self.check_output(item.path, output, overwrite)
        meta = item.metadata
        keep = self.applied_cuts(media_id) if use_cuts else None
        if keep and self.smartcut_is_stale(item):
            raise StudioError("The source file changed after its audio was analysed. Analyse it again in Smart Cut "
                              "before exporting with cuts.")
        out_w, out_h = canvas or (meta.display_width, meta.display_height)
        effects = self.effects(media_id) if use_effects else EditEffects()
        effects.validate()
        ass = None
        if burn_captions:
            segments = output_captions(self.edited_captions(media_id, None), keep, effects.video_factor)
            if not segments:
                raise StudioError("There are no captions to burn in (none exist, or all are in removed parts).")
            ass = build_ass(segments, item.effective_caption_style, out_w, out_h, title=item.name)
        if preview:
            stage = {"type": "scale", "height": 360}
            ass = None
        payload = {
            "path": item.path, "media_id": media_id, "output": output, "overwrite": overwrite,
            "duration": meta.duration, "fps": meta.fps, "has_audio": meta.has_audio,
            "audio_codec": meta.audio_codec, "keep_ranges": [list(r) for r in keep] if keep else None,
            "stage": stage, "ass": ass, "output_settings": settings.to_dict(), "preview": preview,
            "effects": effects.to_dict() if effects.active else None,
        }
        return self.services.jobs.submit(RENDER_JOB, title or f"Export edited video: {item.name}", payload,
                                         project_id=self.project.id)

    def final_operations(self, media_id: str) -> dict:
        """What the Edit page final export includes, from the video's saved settings.

        The switches are the same settings the other pages use: Smart Cut cuts and caption burn-in
        (``ResizeSettings.use_cuts``/``burn_captions``), Resize & Crop or Auto Reframe (Resize & Crop
        settings with ``enabled``; mode "reframe" = Auto Reframe) and the speed/audio effects. An
        operation is only included when its data exists (applied cuts, captions).
        """
        item = self.media(media_id)
        rs = item.resize
        cuts = self.applied_cuts(media_id)
        has_captions = bool(item.captions and item.captions.segments)
        use_resize = rs is not None and rs.enabled
        # Captions are burned in when they exist, unless the user switched burn-in off (Captions tool
        # or Resize & Crop page). Switches created implicitly by another control do not count as a choice.
        burn_requested = bool(rs and rs.burn_captions) or (has_captions and not (rs and rs.burn_set))
        return {"use_cuts": bool(cuts) and (rs.use_cuts if rs else True), "cuts_available": bool(cuts),
                "resize_configured": rs is not None,
                "burn_captions": has_captions and burn_requested, "captions_available": has_captions,
                "captions_missing": burn_requested and not has_captions,
                "use_resize": use_resize, "reframe": use_resize and rs.mode == "reframe",
                "effects": self.effects(media_id)}

    def submit_final_frame(self, media_id: str, at_seconds: float) -> Job:
        """Render the source frame at ``at_seconds`` with the final export's framing and captions."""
        item = self.media(media_id)
        ops = self.final_operations(media_id)
        stage, canvas = None, (item.metadata.display_width, item.metadata.display_height)
        if ops["use_resize"]:
            stage, canvas, _notes = self.resize_stage(media_id)
        ass = None
        if ops["burn_captions"]:
            ass = build_ass(item.captions.segments, item.effective_caption_style, *canvas, title=item.name)
        payload = {"path": item.path, "media_id": media_id, "at": at_seconds, "stage": stage, "ass": ass}
        return self.services.jobs.submit(RENDER_FRAME_JOB, f"Final frame preview: {item.name}", payload,
                                         project_id=self.project.id)

    def update_resize(self, media_id: str, **changes) -> ResizeSettings:
        """Change some Resize & Crop settings (a video without any starts with resizing switched off)."""
        item = self.media(media_id)
        settings = ResizeSettings.from_dict(item.resize.to_dict()) if item.resize else ResizeSettings(enabled=False)
        for key, value in changes.items():
            if key not in ResizeSettings.__dataclass_fields__:
                raise KeyError(key)
            setattr(settings, key, value)
        self.set_resize_settings(media_id, settings)
        return settings

    def submit_combined_export(self, media_id: str, output: str, settings: OutputSettings, *, use_cuts: bool,
                               use_resize: bool, burn_captions: bool, use_effects: bool,
                               overwrite: bool = False) -> Job:
        """One export with every chosen operation: Smart Cut cuts, Resize & Crop or Auto Reframe
        (the Resize & Crop page settings), captions and speed/audio effects.

        A switched-on operation that cannot be applied stops the export with the reason; it is never
        left out silently.
        """
        item = self.media(media_id)
        if burn_captions and not (item.captions and item.captions.segments):
            raise StudioError("Burning in captions is on, but this video has no captions. Generate or import them "
                              "(Captions tool), or turn burn-in off.")
        effects = self.effects(media_id)
        if use_effects and effects.audio_active:
            if item.metadata is not None and not item.metadata.has_audio:
                raise StudioError("Audio processing is on, but this video has no audio track. Turn audio processing "
                                  "off (Audio tool).")
            if settings.audio == "none":
                raise StudioError("Audio processing is on, but the export settings say 'No audio'. Choose an audio "
                                  "mode in the export settings or turn audio processing off.")
        stage, canvas = None, None
        if use_resize:
            stage, canvas, _notes = self.resize_stage(media_id)
        return self.submit_render(media_id, output, settings, use_cuts=use_cuts, burn_captions=burn_captions,
                                  stage=stage, canvas=canvas, overwrite=overwrite, use_effects=use_effects,
                                  title=f"Export final video: {Path(output).name}")

    # -- resize & crop ------------------------------------------------------------------
    def resize_settings(self, media_id: str) -> ResizeSettings:
        item = self.media(media_id)
        return ResizeSettings.from_dict(item.resize.to_dict()) if item.resize else ResizeSettings()

    def set_resize_settings(self, media_id: str, settings: ResizeSettings) -> None:
        settings.validate()
        self.media(media_id).resize = ResizeSettings.from_dict(settings.to_dict())
        self.project.dirty = True
        self.resize_changed.emit(media_id)
        self.dirty_changed.emit(True)

    def resize_layout(self, media_id: str, settings: ResizeSettings | None = None) -> Layout:
        item = self.media(media_id)
        if item.metadata is None:
            raise StudioError("Video information has not been read yet.")
        s = settings or self.resize_settings(media_id)
        return compute_layout(item.metadata.display_width, item.metadata.display_height, s)

    def reframe_path_for(self, media_id: str, out_w: int, out_h: int, burn_captions: bool) -> CropPath:
        """The Auto Reframe crop path for an output of ``out_w``×``out_h`` (uses the stored analysis)."""
        item = self.media(media_id)
        preset = next((name for name, ratio in ASPECT_PRESETS.items()
                       if ratio and abs(ratio[0] / ratio[1] - out_w / out_h) < 0.01), None)
        if preset is None:
            raise LayoutError("Auto Reframe works for 16:9, 9:16, 1:1 and 4:5 outputs. Choose one of those sizes "
                              "or another mode.")
        state = self.reframe_state(media_id)
        settings = ReframeSettings.from_dict(state.settings.to_dict())
        settings.preset, settings.output_width, settings.output_height = preset, out_w, out_h
        if settings.tracks_subjects and state.analysis is None:
            raise LayoutError("Auto Reframe needs a face/person analysis. Analyse this video on the Auto Reframe "
                              "page first (or choose Center Crop there).")
        reasons = reanalysis_reasons(state.analysis, settings) if settings.tracks_subjects else []
        if reasons:  # never use an analysis made for another mode silently
            raise LayoutError("Auto Reframe needs a new analysis: " + " ".join(reasons) + " Analyse the video "
                              "again (Analyse Faces).")
        meta = item.metadata
        band = caption_band_from_style(item.effective_caption_style) if burn_captions and item.captions else None
        return compute_crop_path(state.analysis, settings, meta.display_width, meta.display_height, meta.duration,
                                 caption_band=band)

    def resize_stage(self, media_id: str, settings: ResizeSettings | None = None) -> tuple[dict, tuple[int, int], list[str]]:
        """``(stage, canvas size, notes)`` for the render pipeline."""
        item = self.media(media_id)
        s = settings or self.resize_settings(media_id)
        layout = self.resize_layout(media_id, s)
        canvas = (layout.out_w, layout.out_h)
        meta = item.metadata
        if s.mode == "reframe":
            path = self.reframe_path_for(media_id, *canvas, s.burn_captions)
            stage = {"type": "reframe", "src_w": path.src_w, "src_h": path.src_h, "crop_w": path.crop_w,
                     "crop_h": path.crop_h, "keyframes": path.keyframes(), "out_w": canvas[0], "out_h": canvas[1],
                     "fps": meta.fps, "duration": meta.duration}
            return stage, canvas, list(path.warnings)
        return {"type": "layout", "layout": layout.to_dict()}, canvas, layout_notes(
            layout, meta.display_width, meta.display_height)

    def submit_resize_export(self, media_id: str, output: str, output_settings: OutputSettings,
                             overwrite: bool = False) -> Job:
        s = self.resize_settings(media_id)
        stage, canvas, _notes = self.resize_stage(media_id, s)
        return self.submit_render(media_id, output, output_settings, use_cuts=s.use_cuts,
                                  burn_captions=s.burn_captions, stage=stage, canvas=canvas, overwrite=overwrite,
                                  title=f"Export {canvas[0]}x{canvas[1]}: {Path(output).name}")

    def submit_resize_frame(self, media_id: str, at_seconds: float) -> Job:
        """Render the output frame at a source time exactly as the export would."""
        item = self.media(media_id)
        s = self.resize_settings(media_id)
        stage, canvas, _notes = self.resize_stage(media_id, s)
        ass = None
        if s.burn_captions and item.captions and item.captions.segments:
            ass = build_ass(item.captions.segments, item.effective_caption_style, *canvas, title=item.name)
        payload = {"path": item.path, "media_id": media_id, "at": at_seconds, "stage": stage, "ass": ass}
        return self.services.jobs.submit(RENDER_FRAME_JOB, f"Resize frame preview: {item.name}", payload,
                                         project_id=self.project.id)

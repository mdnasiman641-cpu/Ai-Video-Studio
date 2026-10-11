"""Smart Cut analysis and the shared edited-video export (``render_video``).

The export payload snapshots everything: the applied cut ranges, the video stage (resize
layout or reframe crop), caption ASS text already remapped to the edited timeline and sized
for the output canvas, and the encode settings. A queued export is therefore not affected
by later edits and survives a restart.

Outputs are protected: the source is never written, an existing file is only replaced when
the user chose that, two jobs can never write the same file at the same time, and FFmpeg
writes to a ``.part`` file that becomes the output only after a successful encode (so a
crash or cancel never leaves a truncated video under the final name).
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from typing import Callable

from ai_video_studio.core import paths
from ai_video_studio.core.errors import MediaError, StudioError
from ai_video_studio.core.fs import remove_tree
from ai_video_studio.core.process import run_process, run_streaming
from ai_video_studio.jobs.caption_jobs import SUBTITLE_FILE, job_temp_dir
from ai_video_studio.jobs.encode import encode_with_fallback, ffmpeg_error, partial_path
from ai_video_studio.jobs.manager import JobContext
from ai_video_studio.jobs.models import Job
from ai_video_studio.captions.style import CaptionStyle
from ai_video_studio.jobs.reframe_jobs import run_tracking_analysis
from ai_video_studio.reframe.framing import caption_band_from_style, compute_crop_path, resolve_output_size
from ai_video_studio.reframe.model import ReframeSettings
from ai_video_studio.render.output import OutputSettings
from ai_video_studio.render.pipeline import (
    audio_plan,
    build_filter_graph,
    is_full_clip,
    kept_duration,
    render_args,
    snap_ranges,
)
from ai_video_studio.render.stages import stage_from_payload
from ai_video_studio.smartcut.levels import AudioLevels, LevelsParser, levels_args

log = logging.getLogger(__name__)

SMARTCUT_ANALYZE_JOB = "smartcut_analyze"
RENDER_JOB = "render_video"
RENDER_FRAME_JOB = "render_frame_preview"

_writing: set[str] = set()
_writing_lock = threading.Lock()


def output_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def make_smartcut_analyze_handler(get_ffmpeg: Callable[[], Path]):
    def handle(job: Job, ctx: JobContext) -> dict:
        p = job.payload
        source = Path(p["path"])
        if not source.is_file():
            raise MediaError(f"Source video not found: {source}")
        if p.get("has_audio") is False:
            raise MediaError("This video has no audio track, so there is no silence to detect.")
        duration = float(p.get("duration") or 0)
        parser = LevelsParser()

        def on_line(line: str) -> None:
            t = parser.feed(line)
            if t is not None and duration > 0 and len(parser.peak) % 40 == 0:
                ctx.report_progress(min(0.99, t / duration))

        stat = source.stat()
        result = run_streaming(levels_args(get_ffmpeg(), str(source)), on_line, cancel_event=ctx.cancel_event)
        if result.returncode != 0:
            detail = result.stderr.lower()
            if "matches no streams" in detail or "does not contain any stream" in detail:
                raise MediaError("This video has no audio track, so there is no silence to detect.")
            raise MediaError(f"Audio analysis failed: {ffmpeg_error(result.stderr, 'unknown FFmpeg error')}")
        if not parser.peak:
            raise MediaError("The audio track contains no samples.")
        levels = AudioLevels(parser.peak, parser.rms, duration or len(parser.peak) * 0.025,
                             source_size=stat.st_size, source_mtime=round(stat.st_mtime, 3))
        return {"media_id": p.get("media_id"), "levels": levels.to_dict()}

    return handle


def reframe_stage_for(p: dict, ctx: JobContext, ffmpeg: Path, detector_factory=None, vad=None) -> tuple[dict, list[str]]:
    """Analyse the video and build the Auto Reframe stage (batch items with the Auto Reframe operation)."""
    auto = p["reframe_auto"]
    settings = ReframeSettings.from_dict(auto["settings"])
    settings.validate()
    ctx.set_stage("analyzing")
    analysis = None
    if settings.tracks_subjects:
        analysis = run_tracking_analysis(auto["analysis"] | {"path": p["path"]}, ffmpeg, ctx, detector_factory, vad,
                                         progress_range=(0.0, 0.5))
    band = caption_band_from_style(CaptionStyle.from_dict(auto["caption_style"])) if auto.get("caption_style") else None
    width, height = int(auto["analysis"]["width"]), int(auto["analysis"]["height"])
    path = compute_crop_path(analysis, settings, width, height, float(p.get("duration") or 0), caption_band=band)
    out_w, out_h = resolve_output_size(settings, width, height)
    stage = {"type": "reframe", "src_w": path.src_w, "src_h": path.src_h, "crop_w": path.crop_w,
             "crop_h": path.crop_h, "keyframes": path.keyframes(), "out_w": out_w, "out_h": out_h,
             "fps": p.get("fps"), "duration": p.get("duration")}
    notes = list(path.warnings) + (analysis.warnings if analysis else [])
    if analysis is not None and not analysis.tracks:
        notes.append("No face or person was found; the fallback framing was used.")
    return stage, notes


def make_render_handler(get_ffmpeg: Callable[[], Path], detector_factory=None, vad=None):
    def handle(job: Job, ctx: JobContext) -> dict:
        p = job.payload
        source, output = Path(p["path"]), Path(p["output"])
        if not source.is_file():
            raise MediaError(f"Source video not found: {source}")
        if output_key(output) == output_key(source):
            raise StudioError("The output file must be different from the source video.")
        if output.exists() and not p.get("overwrite"):
            raise StudioError(f"{output.name} already exists. Choose another name, or choose to replace it.")
        settings = OutputSettings.from_dict(p.get("output_settings"))
        settings.validate()
        duration = float(p.get("duration") or 0)
        fps = float(p.get("fps") or 0) or None
        ranges = p.get("keep_ranges")
        if ranges:
            ranges = snap_ranges([tuple(r) for r in ranges], fps, duration or None)
            if not ranges:
                raise StudioError("Nothing is left after the cuts.")
            if is_full_clip(ranges, duration):
                ranges = None
        out_duration = kept_duration(ranges) if ranges else (duration or None)
        has_audio = bool(p.get("has_audio", True))
        mode, notes = audio_plan(settings, has_audio, p.get("audio_codec"), bool(ranges))
        notes += settings.notes(fps)
        progress_range = (0.0, 1.0)
        stage_payload = p.get("stage")
        if p.get("reframe_auto") and not stage_payload:
            stage_payload, reframe_notes = reframe_stage_for(p, ctx, get_ffmpeg(), detector_factory, vad)
            notes += reframe_notes
            progress_range = (0.5, 1.0)
        ctx.set_stage("rendering")
        key = output_key(output)
        with _writing_lock:
            if key in _writing:
                raise StudioError(f"Another export is already writing {output.name}.")
            _writing.add(key)
        folder = job_temp_dir(job)
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            stage = stage_from_payload(stage_payload, folder, ranges)
            subtitle_file = None
            if p.get("ass"):
                subtitle_file = SUBTITLE_FILE
                (folder / subtitle_file).write_text(p["ass"], encoding="utf-8")
            graph, _video, audio_label = build_filter_graph(
                keep_ranges=ranges, has_audio=has_audio and mode != "none", video_stage=stage,
                subtitle_file=subtitle_file, target_fps=settings.target_fps())
            ctx.check_cancelled()

            def build(encoder, partial: str) -> list[str]:
                encoder = replace(encoder, audio_bitrate=f"{settings.audio_bitrate}k")
                # Runs inside the job folder: filter arguments only contain plain file names.
                return render_args(get_ffmpeg(), str(source), graph, audio_label, mode, encoder, partial, folder)

            codec, warnings = encode_with_fallback(
                get_ffmpeg(), build, settings.encoder, settings.crf_value, out_duration, output, folder, ctx,
                family=settings.codec, bitrate_kbps=settings.target_bitrate, progress_range=progress_range)
        finally:
            remove_tree(folder)
            with _writing_lock:
                _writing.discard(key)
        log.info("Render finished", extra={"job_id": job.id, "encoder": codec, "cuts": len(ranges or [])})
        return {"media_id": p.get("media_id"), "output": str(output), "encoder": codec,
                "warnings": warnings + notes, "duration": out_duration, "segments": len(ranges or []) or 1,
                "batch_id": p.get("batch_id"), "batch_item": p.get("batch_item")}

    return handle


def make_render_frame_handler(get_ffmpeg: Callable[[], Path]):
    """Render one output frame exactly as the export would (stage + subtitles), on the source timeline."""

    def handle(job: Job, ctx: JobContext) -> dict:
        p = job.payload
        folder = job_temp_dir(job)
        output = paths.temp_dir() / f"frame-preview-{job.id}.png"
        try:
            stage = stage_from_payload(p.get("stage"), folder, None)
            subtitle_file = None
            if p.get("ass"):
                subtitle_file = SUBTITLE_FILE
                (folder / subtitle_file).write_text(p["ass"], encoding="utf-8")
            graph, _v, _a = build_filter_graph(keep_ranges=None, has_audio=False, video_stage=stage,
                                               subtitle_file=subtitle_file)
            args = [str(get_ffmpeg()), "-hide_banner", "-nostdin", "-y", "-i", str(p["path"]),
                    "-ss", f"{max(0.0, float(p['at'])):.3f}", "-filter_complex", graph, "-map", "[vout]",
                    "-frames:v", "1", str(output)]
            try:
                result = run_process(args, timeout=120, cancel_event=ctx.cancel_event, cwd=str(folder))
            except subprocess.TimeoutExpired as exc:
                raise StudioError("Preview render timed out.") from exc
            if result.returncode != 0 or not output.is_file():
                raise StudioError(f"Preview render failed: {ffmpeg_error(result.stderr, 'unknown FFmpeg error')}")
        finally:
            remove_tree(folder)
        return {"media_id": p.get("media_id"), "image": str(output), "at": p["at"]}

    return handle


def leftover_partial(output: str | Path) -> Path | None:
    """A ``.part`` file left by a crash for this output, if any."""
    part = partial_path(Path(output))
    return part if part.exists() else None

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
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from ai_video_studio.core import paths
from ai_video_studio.core.errors import MediaError, StudioError
from ai_video_studio.core.ffmpeg_commands import encoder_label
from ai_video_studio.core.ffmpeg_tools import find_tool, probe_video
from ai_video_studio.core.fs import remove_file, remove_tree
from ai_video_studio.core.process import run_process, run_streaming
from ai_video_studio.captions.models import ModelMissingError
from ai_video_studio.jobs.caption_jobs import SUBTITLE_FILE, job_temp_dir, transcribe_video, transcript_cache_config
from ai_video_studio.jobs.encode import encode_with_fallback, ffmpeg_error, partial_path
from ai_video_studio.jobs.manager import JobContext
from ai_video_studio.jobs.models import Job
from ai_video_studio.captions.ass import build_ass
from ai_video_studio.captions.model import CaptionSegment, output_captions
from ai_video_studio.captions.style import CaptionStyle
from ai_video_studio.jobs.reframe_jobs import run_tracking_analysis
from ai_video_studio.reframe.framing import caption_band_from_style, compute_crop_path, resolve_output_size
from ai_video_studio.reframe.model import ReframeSettings
from ai_video_studio.reframe.model import TrackingAnalysis
from ai_video_studio.render.color import ColorSettings
from ai_video_studio.storage import analysis_cache
from ai_video_studio.render.effects import EditEffects
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
from ai_video_studio.smartcut.model import SmartCutSettings, build_segments, detect_silences, keep_ranges

log = logging.getLogger(__name__)

SMARTCUT_ANALYZE_JOB = "smartcut_analyze"
RENDER_JOB = "render_video"
RENDER_FRAME_JOB = "render_frame_preview"
RENDER_CLIP_JOB = "render_clip_preview"
LEVELS_CACHE_CONFIG = {"levels": 1}   # the levels analysis has no settings
TRACKING_CACHE_VERSION = 1

_writing: set[str] = set()
_writing_lock = threading.Lock()


def output_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


NO_AUDIO = "This video has no audio track, so there is no silence to detect."


def analyse_levels(source: Path, duration: float, ffmpeg: Path, ctx: JobContext,
                   progress_range: tuple[float, float] = (0.0, 1.0)) -> AudioLevels:
    """Measure the audio levels of ``source`` (Smart Cut analysis)."""
    parser = LevelsParser()
    lo, hi = progress_range

    def on_line(line: str) -> None:
        t = parser.feed(line)
        if t is not None and duration > 0 and len(parser.peak) % 40 == 0:
            ctx.report_progress(lo + (hi - lo) * min(0.99, t / duration))

    stat = source.stat()
    result = run_streaming(levels_args(ffmpeg, str(source)), on_line, cancel_event=ctx.cancel_event)
    if result.returncode != 0:
        detail = result.stderr.lower()
        if "matches no streams" in detail or "does not contain any stream" in detail:
            raise MediaError(NO_AUDIO)
        raise MediaError(f"Audio analysis failed: {ffmpeg_error(result.stderr, 'unknown FFmpeg error')}")
    if not parser.peak:
        raise MediaError("The audio track contains no samples.")
    return AudioLevels(parser.peak, parser.rms, duration or len(parser.peak) * 0.025,
                       source_size=stat.st_size, source_mtime=round(stat.st_mtime, 3))


def make_smartcut_analyze_handler(get_ffmpeg: Callable[[], Path]):
    def handle(job: Job, ctx: JobContext) -> dict:
        p = job.payload
        source = Path(p["path"])
        if not source.is_file():
            raise MediaError(f"Source video not found: {source}")
        if p.get("has_audio") is False:
            raise MediaError(NO_AUDIO)
        levels = analyse_levels(source, float(p.get("duration") or 0), get_ffmpeg(), ctx)
        analysis_cache.put("levels", source, LEVELS_CACHE_CONFIG, levels.to_dict())  # Bulk can reuse it
        return {"media_id": p.get("media_id"), "levels": levels.to_dict()}

    return handle


def auto_cut_ranges(p: dict, ctx: JobContext, ffmpeg: Path, progress_range: tuple[float, float]
                    ) -> tuple[list[tuple[float, float]] | None, list[str]]:
    """Automatic Smart Cut for a batch video: detect silence and return the keep ranges."""
    settings = SmartCutSettings.from_dict(p["smartcut_auto"].get("settings"))
    settings.validate()
    if not p.get("has_audio", True):
        return None, ["Smart Cut was skipped: the video has no audio track."]
    ctx.set_stage("analyzing")
    ctx.set_detail(step="Smart Cut: measuring audio levels")
    duration = float(p.get("duration") or 0)
    cached = analysis_cache.get("levels", p["path"], LEVELS_CACHE_CONFIG)
    notes_from_cache = []
    if cached is not None:
        levels = AudioLevels.from_dict(cached)
        notes_from_cache.append("Smart Cut reused the audio levels measured earlier for this file.")
    else:
        levels = analyse_levels(Path(p["path"]), duration, ffmpeg, ctx, progress_range)
        analysis_cache.put("levels", p["path"], LEVELS_CACHE_CONFIG, levels.to_dict())
    duration = duration or levels.duration
    segments = build_segments(detect_silences(levels, settings, duration), duration, settings.min_keep)
    ranges = keep_ranges(segments)
    removed = duration - kept_duration(ranges)
    if not ranges:
        raise StudioError("Smart Cut found no sound to keep (the whole video is below the silence threshold).")
    if removed < 0.05:
        return None, notes_from_cache + [f"Smart Cut found no silence below {settings.threshold_db:g} dB."]
    return ranges, notes_from_cache + [f"Smart Cut removed {removed:.1f} s of silence."]


_filters_cache: dict[str, set[str]] = {}


def ffmpeg_filters(ffmpeg: Path) -> set[str]:
    """Names of the filters this FFmpeg build has (cached per executable)."""
    key = str(ffmpeg)
    if key not in _filters_cache:
        try:
            result = run_process([key, "-hide_banner", "-filters"], timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return set()
        names = set()
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and len(parts[0]) <= 4 and "->" in parts[2]:
                names.add(parts[1])
        _filters_cache[key] = names
    return _filters_cache[key]


def check_filters(ffmpeg: Path, effects: EditEffects | None, color: ColorSettings | None = None) -> None:
    available = ffmpeg_filters(ffmpeg)
    if not available:
        return  # the list could not be read; FFmpeg itself reports a missing filter
    for needed, what in ((effects.required_filters() if effects else set(), "speed or audio"),
                         (color.required_filters() if color else set(), "colour grading or video filter")):
        missing = sorted(needed - available)
        if missing:
            raise StudioError("This FFmpeg build does not include the filter(s) " + ", ".join(missing) +
                              f" needed for the chosen {what} settings. Turn those settings off or use "
                              "a full FFmpeg build.")


def color_for(p: dict) -> ColorSettings | None:
    """The payload's colour grade / video filter, or None when nothing is applied."""
    color = ColorSettings.from_dict(p.get("color"), strict=True) if p.get("color") else None
    return color if color is not None and color.active else None


def color_height(p: dict, stage_payload: dict | None) -> int:
    """The output height (sizes the soft-glow blur like on a 1080p frame)."""
    for value in ((p.get("color") or {}).get("height"), (stage_payload or {}).get("out_h"),
                  ((stage_payload or {}).get("layout") or {}).get("out_h")):
        if value:
            return int(value)
    return 1080


def reframe_stage_for(p: dict, ctx: JobContext, ffmpeg: Path, detector_factory=None, vad=None,
                      progress_range: tuple[float, float] = (0.0, 0.5)) -> tuple[dict, list[str]]:
    """Analyse the video and build the Auto Reframe stage (batch items with the Auto Reframe operation)."""
    auto = p["reframe_auto"]
    settings = ReframeSettings.from_dict(auto["settings"])
    settings.validate()
    ctx.set_stage("analyzing")
    analysis = None
    cache_notes: list[str] = []
    if settings.tracks_subjects:
        config = {k: v for k, v in auto["analysis"].items() if k != "models_dir"} | {"kind": TRACKING_CACHE_VERSION}
        cached = analysis_cache.get("tracking", p["path"], config)
        if cached is not None:
            analysis = TrackingAnalysis.from_dict(cached)
            cache_notes.append("Auto Reframe reused the face/speaker analysis made earlier for this file with the "
                               "same settings.")
        else:
            ctx.set_detail(step="Auto Reframe: detecting faces" + (" and speech" if "speech" in
                                                                     auto["analysis"].get("features", []) else ""))
            analysis = run_tracking_analysis(auto["analysis"] | {"path": p["path"]}, ffmpeg, ctx, detector_factory,
                                             vad, progress_range=progress_range)
            if not any("Haar" in w or "YuNet" in w for w in analysis.warnings):  # not with a fallback detector
                analysis_cache.put("tracking", p["path"], config, analysis.to_dict())
    band = caption_band_from_style(CaptionStyle.from_dict(auto["caption_style"])) if auto.get("caption_style") else None
    width, height = int(auto["analysis"]["width"]), int(auto["analysis"]["height"])
    path = compute_crop_path(analysis, settings, width, height, float(p.get("duration") or 0), caption_band=band)
    out_w, out_h = resolve_output_size(settings, width, height)
    stage = {"type": "reframe", "src_w": path.src_w, "src_h": path.src_h, "crop_w": path.crop_w,
             "crop_h": path.crop_h, "keyframes": path.keyframes(), "out_w": out_w, "out_h": out_h,
             "fps": p.get("fps"), "duration": p.get("duration")}
    notes = cache_notes + list(path.warnings) + (analysis.warnings if analysis else [])
    if analysis is not None and not analysis.tracks:
        notes.append("No face or person was found; the fallback framing was used.")
    return stage, notes


def auto_captions(p: dict, job: Job, ctx: JobContext, ffmpeg: Path, model_factory, cuda_devices,
                  progress_range: tuple[float, float]) -> tuple[list[dict], list[str]]:
    """Transcribe a batch video with the Captions page's Whisper transcription (source timeline)."""
    if not p.get("has_audio", True):
        raise MediaError("Captions cannot be generated: this video has no audio track.")
    ctx.set_stage("analyzing")
    config = transcript_cache_config(p["captions_auto"])
    cached = analysis_cache.get("transcript", p["path"], config)
    if cached is not None and cached.get("segments"):
        return cached["segments"], [f"{len(cached['segments'])} caption(s) reused from an earlier transcription of "
                                    f"this file ({cached.get('language')}, same model and language)."]
    ctx.set_detail(step="Captions: transcribing speech (Whisper)")
    folder = paths.temp_dir() / f"job-{job.id}-captions"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        result = transcribe_video(p["captions_auto"] | {"path": p["path"], "duration": p.get("duration"),
                                                        "allow_download": False},
                                  ctx, ffmpeg, folder, model_factory, cuda_devices, progress_range)
    except ModelMissingError as exc:
        model = p["captions_auto"].get("model", "small")
        raise ModelMissingError(
            f"Automatic captions need the Whisper model '{model}', which is not on this computer (batches never "
            "download models). Download it in Settings → Speech recognition models, or choose an installed local "
            "model folder in Settings, then use Retry Failed.") from exc
    except StudioError as exc:
        raise StudioError(f"Automatic captions failed: {exc}") from exc
    finally:
        remove_tree(folder)
    segments = [s.to_dict() for s in result.track.segments]
    if not segments:
        raise MediaError("Automatic captions failed: no speech was recognised, so there are no captions to burn "
                         "in. Turn off automatic captions for this video or check its audio.")
    analysis_cache.put("transcript", p["path"], config, {"segments": segments, "language": result.language,
                                                          "device": result.device})
    return segments, [f"{len(segments)} caption(s) generated ({result.language}, {result.device})."] + list(
        result.warnings)


def verify_output(output: Path, ffprobe: Path | None, expected_duration: float | None, expect: dict | None = None,
                  has_audio: bool | None = None) -> None:
    """Check that an export is a complete, readable video before the job counts as completed.

    The file must exist, be non-empty and readable by FFprobe, with a video stream of the expected
    size (``expect = {"width", "height"}``), a duration close to the expected one, the expected
    frame rate (``expect["fps"]``) and audio when the export should have it. A failing file is
    removed and the job fails with the reason.
    """
    if not output.is_file() or output.stat().st_size == 0:
        remove_file(output)
        raise MediaError("The export produced no file (or an empty one).")
    if ffprobe is None:
        log.warning("FFprobe not found; the exported file was not verified", extra={"output": str(output)})
        return
    try:
        meta = probe_video(output, ffprobe)
    except StudioError as exc:
        remove_file(output)
        raise MediaError(f"The exported file could not be read back ({exc}); it was removed.") from exc
    problems = []
    if not meta.width or not meta.height:
        problems.append("it has no usable video stream")
    elif expect and expect.get("width") and (meta.width, meta.height) != (int(expect["width"]), int(expect["height"])):
        problems.append(f"it is {meta.width}×{meta.height} instead of {expect['width']}×{expect['height']}")
    if expected_duration and meta.duration < min(0.5, expected_duration) * 0.5:
        problems.append("it is much shorter than expected")
    elif expected_duration and expected_duration > 2 and meta.duration < expected_duration * 0.9:
        problems.append(f"it is {meta.duration:.1f} s long instead of about {expected_duration:.1f} s")
    if expect and expect.get("fps") and meta.fps and abs(meta.fps - float(expect["fps"])) > 0.5:
        problems.append(f"it has {meta.fps:.3g} fps instead of {float(expect['fps']):g}")
    if has_audio and not meta.has_audio:
        problems.append("its audio track is missing")
    if problems:
        remove_file(output)
        raise MediaError("The exported file failed the final check (" + "; ".join(problems) + "); it was removed. "
                         "Check the FFmpeg log in the logs folder.")


def _sibling_ffprobe(ffmpeg: Path) -> Path | None:
    candidate = Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix)
    return candidate if candidate.is_file() else find_tool("ffprobe")


def make_render_handler(get_ffmpeg: Callable[[], Path], detector_factory=None, vad=None, model_factory=None,
                        cuda_devices: int | None = None, get_ffprobe: Callable[[], Path] | None = None):
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
        effects = EditEffects.from_dict(p.get("effects"), strict=True)
        color = color_for(p)
        if effects.active or color:
            check_filters(get_ffmpeg(), effects if effects.active else None, color)
        duration = float(p.get("duration") or 0)
        fps = float(p.get("fps") or 0) or None
        ranges = p.get("keep_ranges")
        notes: list[str] = []
        start = 0.0
        caption_segments = (p.get("captions") or {}).get("segments")
        timings: dict[str, float] = {}
        clock = time.monotonic()

        def lap(name: str) -> None:
            nonlocal clock
            now = time.monotonic()
            timings[name] = round(now - clock, 2)
            clock = now

        if p.get("captions_auto"):  # generated here, on the source timeline; timed below with the cuts
            start = 0.4
            caption_segments, caption_notes = auto_captions(p, job, ctx, get_ffmpeg(), model_factory, cuda_devices,
                                                            (0.0, start))
            notes += caption_notes
            lap("captions")
        if p.get("smartcut_auto") and not ranges:
            cut_end = start + (0.2 if p.get("reframe_auto") else 0.3) * (1 - start)
            ranges, cut_notes = auto_cut_ranges(p, ctx, get_ffmpeg(), (start, cut_end))
            start = cut_end
            notes += cut_notes
            lap("smart_cut")
        if ranges:
            ranges = snap_ranges([tuple(r) for r in ranges], fps, duration or None)
            if not ranges:
                raise StudioError("Nothing is left after the cuts.")
            if is_full_clip(ranges, duration):
                ranges = None
        edited_duration = kept_duration(ranges) if ranges else (duration or None)
        out_duration = effects.output_duration(edited_duration)
        has_audio = bool(p.get("has_audio", True))
        mode, audio_notes = audio_plan(settings, has_audio, p.get("audio_codec"), bool(ranges), effects.changes_audio)
        notes += audio_notes + settings.notes(fps) + (["Speed and audio: " + ", ".join(effects.summary()) + "."]
                                                      if effects.summary() else [])
        if color:
            notes.append("Colour: " + ", ".join(color.summary()) + ".")
        stage_payload = p.get("stage")
        if p.get("reframe_auto") and not stage_payload:
            stage_payload, reframe_notes = reframe_stage_for(p, ctx, get_ffmpeg(), detector_factory, vad,
                                                             (start, start + (1 - start) / 2))
            notes += reframe_notes
            start = start + (1 - start) / 2
            lap("auto_reframe")
        progress_range = (start, 1.0)
        ass = p.get("ass")
        if p.get("captions"):  # captions timed here, after the cuts are known (cuts, then speed: once)
            c = p["captions"]
            segments = output_captions([CaptionSegment.from_dict(d) for d in caption_segments or []], ranges,
                                       effects.video_factor)
            if segments:
                ass = build_ass(segments, CaptionStyle.from_dict(c.get("style")), int(c["width"]), int(c["height"]),
                                title=c.get("title") or "AI Video Studio captions", animation=c.get("animation"))
            elif p.get("captions_auto"):  # captions are required: never export without them
                raise StudioError("Automatic captions: every caption is inside a part removed by Smart Cut, so "
                                  "there are no captions to burn in.")
            else:
                notes.append("No captions are left after the cuts; exported without captions.")
        ctx.set_stage("rendering")
        ctx.set_detail(step="Rendering and encoding")
        clock = time.monotonic()
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
            if ass:
                subtitle_file = SUBTITLE_FILE
                (folder / subtitle_file).write_text(ass, encoding="utf-8")
            graph, _video, audio_label = build_filter_graph(
                keep_ranges=ranges, has_audio=has_audio and mode != "none", video_stage=stage,
                subtitle_file=subtitle_file, target_fps=settings.target_fps(), video_filters=effects.video_filters(),
                source_fps=fps, audio_filters=effects.audio_filters(out_duration) if mode == "filtered" else None,
                color_stage=color.graph(color_height(p, stage_payload)) if color else None)
            ctx.check_cancelled()

            def build(encoder, partial: str) -> list[str]:
                encoder = replace(encoder, audio_bitrate=f"{settings.audio_kbps}k")
                # Runs inside the job folder: filter arguments only contain plain file names.
                return render_args(get_ffmpeg(), str(source), graph, audio_label, mode, encoder, partial, folder,
                                   hw_decode=settings.hw_decode == "auto")

            codec, warnings = encode_with_fallback(
                get_ffmpeg(), build, settings.encoder, settings.crf_value, out_duration, output, folder, ctx,
                family=settings.codec, bitrate_kbps=settings.target_bitrate, progress_range=progress_range,
                speed=settings.speed, cpu_preset=settings.cpu_preset, allow_fallback=settings.allow_fallback)
        finally:
            remove_tree(folder)
            with _writing_lock:
                _writing.discard(key)
        lap("render")
        ctx.set_detail(step="Checking the exported file")
        verify_output(output, get_ffprobe() if get_ffprobe else _sibling_ffprobe(get_ffmpeg()), out_duration,
                      expect=p.get("expect"), has_audio=has_audio and mode != "none")
        lap("verify")
        log.info("Render finished", extra={"job_id": job.id, "encoder": codec, "cuts": len(ranges or []),
                                           "speed": effects.video_factor, "timings": timings,
                                           "color": ", ".join(color.summary()) if color else "none"})
        return {"media_id": p.get("media_id"), "output": str(output), "encoder": codec,
                "encoder_label": encoder_label(codec), "timings": timings, "mode": settings.describe(),
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
            color = color_for(p)
            graph, _v, _a = build_filter_graph(keep_ranges=None, has_audio=False, video_stage=stage,
                                               subtitle_file=subtitle_file,
                                               color_stage=color.graph(color_height(p, p.get("stage")))
                                               if color else None)
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


def make_render_clip_handler(get_ffmpeg: Callable[[], Path]):
    """Render a short, small preview clip with burned-in captions (caption animation preview).

    ``ass`` is timed from the clip start and sized for ``height``; the clip is written to the
    temp folder (never next to the source) and is a preview, not an export.
    """

    def handle(job: Job, ctx: JobContext) -> dict:
        p = job.payload
        start, end = max(0.0, float(p["start"])), float(p["end"])
        if end - start <= 0.05:
            raise StudioError("The preview clip is too short.")
        source = Path(p["path"])
        if not source.is_file():
            raise MediaError(f"Source video not found: {source}")
        height = max(2, int(p.get("height") or 540) // 2 * 2)
        folder = job_temp_dir(job)
        previews = paths.temp_dir() / "previews"
        previews.mkdir(parents=True, exist_ok=True)
        output = previews / f"clip-{job.id}.mp4"
        try:
            (folder / SUBTITLE_FILE).write_text(p["ass"], encoding="utf-8")
            color = color_for(p)
            stage = lambda src, dst: f"[{src}]scale=-2:{height}[{dst}]"  # noqa: E731
            graph, _v, _a = build_filter_graph(keep_ranges=None, has_audio=False, video_stage=stage,
                                               subtitle_file=SUBTITLE_FILE,
                                               color_stage=color.graph(height) if color else None)
            args = [str(get_ffmpeg()), "-hide_banner", "-nostdin", "-y", "-ss", f"{start:.3f}", "-i", str(source),
                    "-t", f"{end - start:.3f}", "-filter_complex", graph, "-map", "[vout]", "-map", "0:a:0?",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-b:a", "128k", str(output)]
            try:
                result = run_process(args, timeout=300, cancel_event=ctx.cancel_event, cwd=str(folder))
            except subprocess.TimeoutExpired as exc:
                raise StudioError("The preview clip timed out.") from exc
            if result.returncode != 0 or not output.is_file():
                remove_file(output)
                raise StudioError(f"Preview clip failed: {ffmpeg_error(result.stderr, 'unknown FFmpeg error')}")
        finally:
            remove_tree(folder)
        return {"media_id": p.get("media_id"), "clip": str(output), "start": start}

    return handle


def leftover_partial(output: str | Path) -> Path | None:
    """A ``.part`` file left by a crash for this output, if any."""
    part = partial_path(Path(output))
    return part if part.exists() else None

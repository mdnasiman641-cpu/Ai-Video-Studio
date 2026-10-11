"""Auto reframe job handlers: analyse a video, export it reframed, render one exact frame.

Payloads snapshot everything the job needs (crop keyframes, output size, caption ASS
text), so a queued export is not affected by later edits and survives a restart.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from ai_video_studio.core import paths
from ai_video_studio.core.errors import MediaError, StudioError
from ai_video_studio.core.fs import remove_tree
from ai_video_studio.core.process import run_process
from ai_video_studio.jobs.caption_jobs import SUBTITLE_FILE, job_temp_dir
from ai_video_studio.jobs.encode import encode_with_fallback, ffmpeg_error
from ai_video_studio.jobs.manager import JobContext
from ai_video_studio.jobs.models import Job
from ai_video_studio.reframe.analysis import analyze_video
from ai_video_studio.reframe.detection import DetectorSet, build_detectors
from ai_video_studio.reframe.export import (
    CROP_COMMAND_FILE,
    reframe_args,
    reframe_filter,
    reframe_frame_args,
    sendcmd_script,
)
from ai_video_studio.reframe.framing import CropPath
from ai_video_studio.reframe.model import TrackingAnalysis
from ai_video_studio.reframe.speech import analyse_speech, extract_speech_audio

REFRAME_ANALYZE_JOB = "reframe_analyze"
REFRAME_EXPORT_JOB = "reframe_export"
REFRAME_PREVIEW_JOB = "reframe_frame_preview"

DetectorFactory = Callable[[dict], DetectorSet]


def default_detector_factory(payload: dict) -> DetectorSet:
    models_dir = Path(payload.get("models_dir") or paths.data_dir() / "models")
    return build_detectors(models_dir, bool(payload.get("detect_persons", True)),
                           min_confidence=float(payload.get("face_threshold", 0.6)))


def payload_features(p: dict) -> set[str]:
    features = set(p.get("features") or ["faces"])
    if p.get("detect_persons") and "faces" in features and "features" not in p:
        features.add("persons")
    return features


def make_reframe_analyze_handler(get_ffmpeg: Callable[[], Path], detector_factory: DetectorFactory | None = None,
                                 vad=None):
    """``vad`` replaces the Silero voice-activity model (tests)."""
    factory = detector_factory or default_detector_factory

    def handle(job: Job, ctx: JobContext) -> dict:
        p = job.payload
        source = Path(p["path"])
        if not source.is_file():
            raise MediaError(f"Source video not found: {source}")
        ffmpeg = get_ffmpeg()
        features = payload_features(p)
        want_speech = "speech" in features
        video_share = 0.8 if want_speech else 1.0
        stat = source.stat()
        width, height = int(p["width"]), int(p["height"])
        duration = float(p.get("duration") or 0)
        sample_fps = float(p.get("sample_fps", 5))
        face_threshold = float(p.get("face_threshold", 0.6))
        detector_warnings: list[str] = []
        if features & {"faces", "persons", "mouth"}:
            detectors = factory(p)
            detector_warnings = list(detectors.warnings)
            ctx.check_cancelled()
            analysis = analyze_video(
                ffmpeg, str(source), width, height, duration, sample_fps, detectors, detectors.description,
                progress=lambda f: ctx.report_progress(f * video_share), cancel_event=ctx.cancel_event,
                features=features, face_threshold=face_threshold,
            )
        else:
            analysis = TrackingAnalysis(width=width, height=height, duration=duration, sample_fps=sample_fps,
                                        frames_analyzed=0, detector="", features=[], face_threshold=face_threshold)
        if want_speech:
            folder = job_temp_dir(job)
            try:
                wav = folder / "speech.wav"
                extract_speech_audio(ffmpeg, str(source), str(wav), cancel_event=ctx.cancel_event)
                ctx.report_progress(video_share + 0.05)
                kwargs = {"vad": vad, "detector": "test VAD"} if vad is not None else {}
                analysis.speech = analyse_speech(
                    str(wav), progress=lambda f: ctx.report_progress(video_share + 0.05 + 0.15 * f * 0.999),
                    check_cancelled=ctx.check_cancelled, **kwargs)
            finally:
                remove_tree(folder)
            analysis.features = sorted(set(analysis.features) | {"speech"})
            if not any(v >= 0.5 for v in analysis.speech.prob):
                analysis.notes.append("No speech was detected in the audio.")
        analysis.warnings = detector_warnings + analysis.warnings
        analysis.source_size, analysis.source_mtime = stat.st_size, round(stat.st_mtime, 3)
        return {"media_id": p.get("media_id"), "analysis": analysis.to_dict(),
                "tracks": len(analysis.tracks), "features": analysis.features, "warnings": analysis.warnings}

    return handle


def crop_path_from_payload(p: dict) -> CropPath:
    return CropPath.from_keyframes(int(p["src_w"]), int(p["src_h"]), int(p["crop_w"]), int(p["crop_h"]),
                                   p["keyframes"])


def make_reframe_export_handler(get_ffmpeg: Callable[[], Path]):
    def handle(job: Job, ctx: JobContext) -> dict:
        p = job.payload
        source, output = Path(p["path"]), Path(p["output"])
        if not source.is_file():
            raise MediaError(f"Source video not found: {source}")
        if output.resolve() == source.resolve():
            raise StudioError("The output file must be different from the source video.")
        path = crop_path_from_payload(p)
        out_w, out_h = int(p["out_w"]), int(p["out_h"])
        if out_w % 2 or out_h % 2 or out_w <= 0 or out_h <= 0:
            raise StudioError("The output size must be positive even numbers.")
        output.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg = get_ffmpeg()
        folder = job_temp_dir(job)
        try:
            command_file = None
            if not path.is_static:
                command_file = CROP_COMMAND_FILE
                script = sendcmd_script(path, float(p.get("fps") or 0), float(p.get("duration") or path.times[-1]))
                (folder / command_file).write_text(script, encoding="utf-8")
            subtitle_file = None
            if p.get("ass"):
                subtitle_file = SUBTITLE_FILE
                (folder / subtitle_file).write_text(p["ass"], encoding="utf-8")
            video_filter = reframe_filter(path, out_w, out_h, subtitle_file, command_file)
            ctx.check_cancelled()

            def build(encoder, partial: str) -> list[str]:
                # Runs inside the job folder: filter arguments only contain plain file names.
                return reframe_args(ffmpeg, str(source), video_filter, partial, encoder,
                                    p.get("audio_codec"), bool(p.get("has_audio", True)))

            codec, warnings = encode_with_fallback(ffmpeg, build, p.get("encoder", "auto"), int(p.get("quality", 20)),
                                                   p.get("duration"), output, folder, ctx)
        finally:
            remove_tree(folder)
        return {"media_id": p.get("media_id"), "output": str(output), "encoder": codec, "warnings": warnings,
                "size": [out_w, out_h], "moving_crop": command_file is not None}

    return handle


def make_reframe_preview_handler(get_ffmpeg: Callable[[], Path]):
    def handle(job: Job, ctx: JobContext) -> dict:
        p = job.payload
        path = crop_path_from_payload(p)
        folder = job_temp_dir(job)
        output = paths.temp_dir() / f"reframe-preview-{job.id}.png"
        subtitle_file = None
        if p.get("ass"):
            subtitle_file = SUBTITLE_FILE
            (folder / subtitle_file).write_text(p["ass"], encoding="utf-8")
        args = reframe_frame_args(get_ffmpeg(), p["path"], float(p["at"]), path, int(p["out_w"]), int(p["out_h"]),
                                  subtitle_file, str(output))
        try:
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

"""Caption-related job handlers.

Payloads contain everything a job needs (paths, settings, a snapshot of the captions
and style), so a queued job behaves the same after an application restart and is not
affected by edits made after it was queued.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from ai_video_studio.ai.providers import JsonProvider, ProviderConfig
from ai_video_studio.captions.correction import request_corrections
from ai_video_studio.captions.transcriber import TranscriptionOptions, transcribe_file
from ai_video_studio.core import paths
from ai_video_studio.core.errors import MediaError, StudioError
from ai_video_studio.core.ffmpeg_commands import (
    EncoderSettings,
    ProgressParser,
    audio_extract_args,
    burn_in_args,
    choose_video_encoder,
    frame_preview_args,
)
from ai_video_studio.core.process import run_process, run_streaming
from ai_video_studio.jobs.manager import JobContext
from ai_video_studio.jobs.models import Job

log = logging.getLogger(__name__)

TRANSCRIBE_JOB = "transcribe"
BURN_IN_JOB = "burn_in"
FRAME_PREVIEW_JOB = "caption_frame_preview"
CORRECTION_JOB = "caption_correction"

SUBTITLE_FILE = "captions.ass"


def job_temp_dir(job: Job) -> Path:
    folder = paths.temp_dir() / f"job-{job.id}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _ffmpeg_error(result_stderr: str, fallback: str) -> str:
    lines = [ln.strip() for ln in result_stderr.splitlines() if ln.strip()]
    return lines[-1] if lines else fallback


def extract_audio(ffmpeg: Path, source: str, output_wav: str, duration: float | None,
                  ctx: JobContext, weight: float) -> None:
    parser = ProgressParser(duration)

    def on_line(line: str) -> None:
        fraction = parser.feed(line)
        if fraction is not None:
            ctx.report_progress(fraction * weight)

    result = run_streaming(audio_extract_args(ffmpeg, source, output_wav), on_line, cancel_event=ctx.cancel_event)
    if result.returncode != 0:
        detail = result.stderr.lower()
        if "matches no streams" in detail or "does not contain any stream" in detail:
            raise MediaError("This video has no audio track, so there is nothing to transcribe.")
        raise MediaError(f"Audio extraction failed: {_ffmpeg_error(result.stderr, 'unknown FFmpeg error')}")


def make_transcribe_handler(get_ffmpeg: Callable[[], Path], model_factory=None, cuda_devices: int | None = None):
    def handle(job: Job, ctx: JobContext) -> dict:
        p = job.payload
        options = TranscriptionOptions(
            model=p.get("model", "small"),
            language=p.get("language", "auto"),
            device=p.get("device", "auto"),
            models_dir=p.get("models_dir") or str(paths.data_dir() / "models"),
            model_path=p.get("model_path") or None,
            max_chars=int(p.get("max_chars", 42)),
            max_duration=float(p.get("max_seconds", 6)),
        )
        options.validate()
        if not Path(p["path"]).is_file():
            raise MediaError(f"Source video not found: {p['path']}")
        folder = job_temp_dir(job)
        wav = folder / "audio.wav"
        try:
            extract_audio(get_ffmpeg(), p["path"], str(wav), p.get("duration"), ctx, weight=0.1)
            ctx.report_progress(0.1)
            result = transcribe_file(
                str(wav), options,
                progress=lambda f: ctx.report_progress(0.1 + 0.9 * f),
                check_cancelled=ctx.check_cancelled,
                model_factory=model_factory,
                cuda_devices=cuda_devices,
            )
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        return {
            "media_id": p.get("media_id"),
            "track": result.track.to_dict(),
            "language": result.language,
            "language_probability": round(result.language_probability, 3),
            "device": result.device,
            "compute_type": result.compute_type,
            "model": result.model,
            "warnings": result.warnings,
        }

    return handle


def make_burn_in_handler(get_ffmpeg: Callable[[], Path]):
    def handle(job: Job, ctx: JobContext) -> dict:
        p = job.payload
        source, output = Path(p["path"]), Path(p["output"])
        if not source.is_file():
            raise MediaError(f"Source video not found: {source}")
        if output.resolve() == source.resolve():
            raise StudioError("The output file must be different from the source video.")
        output.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg = get_ffmpeg()
        codec, reason = choose_video_encoder(p.get("encoder", "auto"), ffmpeg)
        warnings = [] if codec == "h264_nvenc" or p.get("encoder") == "libx264" else [reason]
        folder = job_temp_dir(job)
        (folder / SUBTITLE_FILE).write_text(p["ass"], encoding="utf-8")
        partial = output.with_name(output.stem + ".part" + output.suffix)
        try:
            result = _encode(ffmpeg, p, codec, folder, partial, ctx)
            if result.returncode != 0 and codec == "h264_nvenc":
                warnings.append("NVENC encoding failed; retried with the CPU encoder (libx264): "
                                + _ffmpeg_error(result.stderr, "unknown error"))
                log.warning("NVENC encode failed, retrying with libx264", extra={"job_id": job.id})
                codec = "libx264"
                result = _encode(ffmpeg, p, codec, folder, partial, ctx)
            if result.returncode != 0:
                raise StudioError(f"Export failed: {_ffmpeg_error(result.stderr, 'unknown FFmpeg error')}")
            os.replace(partial, output)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
            if partial.exists():
                try:
                    partial.unlink()
                except OSError:
                    pass
        return {"media_id": p.get("media_id"), "output": str(output), "encoder": codec, "warnings": warnings}

    return handle


def _encode(ffmpeg: Path, p: dict, codec: str, folder: Path, partial: Path, ctx: JobContext):
    encoder = EncoderSettings(codec=codec, quality=int(p.get("quality", 20)))
    args = burn_in_args(ffmpeg, p["path"], SUBTITLE_FILE, str(partial), encoder,
                        p.get("audio_codec"), bool(p.get("has_audio", True)))
    parser = ProgressParser(p.get("duration"))

    def on_line(line: str) -> None:
        fraction = parser.feed(line)
        if fraction is not None:
            ctx.report_progress(min(fraction, 0.999))

    # Run inside the job folder so the subtitles filter gets a plain relative file name.
    return run_streaming(args, on_line, cancel_event=ctx.cancel_event, cwd=str(folder))


def make_frame_preview_handler(get_ffmpeg: Callable[[], Path]):
    def handle(job: Job, ctx: JobContext) -> dict:
        p = job.payload
        folder = job_temp_dir(job)
        (folder / SUBTITLE_FILE).write_text(p["ass"], encoding="utf-8")
        output = paths.temp_dir() / f"preview-{job.id}.png"
        args = frame_preview_args(get_ffmpeg(), p["path"], SUBTITLE_FILE, float(p["at"]), str(output))
        try:
            try:
                result = run_process(args, timeout=120, cancel_event=ctx.cancel_event, cwd=str(folder))
            except subprocess.TimeoutExpired as exc:
                raise StudioError("Preview render timed out.") from exc
            if result.returncode != 0 or not output.is_file():
                raise StudioError(f"Preview render failed: {_ffmpeg_error(result.stderr, 'unknown FFmpeg error')}")
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        return {"media_id": p.get("media_id"), "image": str(output), "at": p["at"]}

    return handle


def make_correction_handler(get_api_key: Callable[[str], str | None], transport=None, sleep=None):
    def handle(job: Job, ctx: JobContext) -> dict:
        p = job.payload
        provider = p["provider"]
        config = ProviderConfig(provider=provider, model=p["model"], api_key=get_api_key(provider) or "",
                                timeout=float(p.get("timeout", 60)))
        kwargs = {"transport": transport}
        if sleep is not None:
            kwargs["sleep"] = sleep
        client = JsonProvider(config, **kwargs)
        suggestions = request_corrections(client, p["segments"], p.get("language"),
                                          progress=ctx.report_progress, check_cancelled=ctx.check_cancelled)
        return {"media_id": p.get("media_id"), "provider": provider, "model": p["model"],
                "suggestions": suggestions, "checked": len(p["segments"])}

    return handle

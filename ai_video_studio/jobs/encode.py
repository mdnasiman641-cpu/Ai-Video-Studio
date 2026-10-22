"""Shared encoding step for export jobs: encoder choice, hardware → CPU fallback, atomic output."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

from ai_video_studio.core.errors import StudioError
from ai_video_studio.core.ffmpeg_commands import (
    CPU_ENCODERS,
    HW_ENCODERS,
    EncoderSettings,
    ProgressParser,
    choose_video_encoder,
    cpu_encoder_available,
    encoder_family,
    encoder_label,
    normalise_preference,
)
from ai_video_studio.core.fs import remove_file
from ai_video_studio.core.process import run_streaming
from ai_video_studio.jobs.manager import JobContext

log = logging.getLogger(__name__)


def ffmpeg_error(stderr: str, fallback: str) -> str:
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    return lines[-1] if lines else fallback


def partial_path(output: Path) -> Path:
    return output.with_name(output.stem + ".part" + output.suffix)


def encode_with_fallback(
    ffmpeg: Path,
    build_args: Callable[[EncoderSettings, str], list[str]],
    preference: str,
    quality: int,
    duration: float | None,
    output: Path,
    workdir: Path,
    ctx: JobContext,
    family: str = "h264",
    bitrate_kbps: int | None = None,
    progress_range: tuple[float, float] = (0.0, 1.0),
    speed: str = "standard",
    cpu_preset: str = "medium",
    allow_fallback: bool = True,
) -> tuple[str, list[str]]:
    """Encode to ``output`` and return ``(codec, warnings)``; ``codec`` is the encoder really used.

    ``build_args(encoder, partial_output)`` returns the FFmpeg command. A hardware encoder is
    used only when a test encode proved it works (see ``choose_video_encoder``). If the real
    encode still fails on it, it is retried on the CPU only for Auto or when the user allowed
    the fallback; the warning says so with FFmpeg's message. FFmpeg writes to a ``.part`` file
    that replaces ``output`` only after success, and is removed on failure or cancel.
    """
    preference = normalise_preference(preference)
    codec, reason = choose_video_encoder(preference, ffmpeg, family, allow_fallback)
    chosen = preference if preference in HW_ENCODERS else None
    warnings = [] if (encoder_family(codec) != "cpu" and preference == "auto") or reason in (
        "CPU encoder selected",) or (chosen and encoder_family(codec) == chosen) else [reason]
    low, high = progress_range
    partial = partial_path(output)

    def run(selected: str):
        parser = ProgressParser(duration)
        ctx.set_detail(encoder=encoder_label(selected), speed=None)

        def on_line(line: str) -> None:
            fraction = parser.feed(line)
            if line.startswith("speed=") and parser.speed:
                ctx.set_detail(speed=parser.speed, fps=parser.fps)
            if fraction is not None:
                ctx.report_progress(low + (high - low) * min(fraction, 0.999))

        args = build_args(EncoderSettings(codec=selected, quality=quality, bitrate_kbps=bitrate_kbps, speed=speed,
                                          cpu_preset=cpu_preset), str(partial))
        log.info("Encoding", extra={"job_id": ctx.job.id, "encoder": selected, "speed": speed})
        return run_streaming(args, on_line, cancel_event=ctx.cancel_event, cwd=str(workdir))

    try:
        result = run(codec)
        cpu = CPU_ENCODERS[family]
        may_fall_back = preference == "auto" or allow_fallback
        if result.returncode != 0 and encoder_family(codec) != "cpu" and may_fall_back and \
                cpu_encoder_available(ffmpeg, family):
            warnings.append(f"{encoder_label(codec)} encoding failed; retried with the CPU encoder ({cpu}): "
                            + ffmpeg_error(result.stderr, "unknown error"))
            log.warning("Hardware encode failed, retrying on the CPU", extra={"job_id": ctx.job.id, "encoder": codec,
                                                                             "error": ffmpeg_error(result.stderr, "")})
            codec = cpu
            result = run(codec)
        if result.returncode != 0:
            raise StudioError(f"Export failed ({encoder_label(codec)}): "
                              f"{ffmpeg_error(result.stderr, 'unknown FFmpeg error')}")
        os.replace(partial, output)
    finally:
        # A cancelled FFmpeg can keep the file locked on Windows for a few seconds after it was
        # killed (an antivirus scan of the new file can add to that), so keep trying for up to 10 s.
        if partial.exists() and not remove_file(partial, attempts=100):
            log.warning("Could not remove partial export", extra={"path": str(partial)})
    return codec, warnings

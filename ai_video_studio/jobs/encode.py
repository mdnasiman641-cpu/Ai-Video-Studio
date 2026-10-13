"""Shared encoding step for export jobs: encoder choice, NVENC → CPU fallback, atomic output."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

from ai_video_studio.core.errors import StudioError
from ai_video_studio.core.ffmpeg_commands import (
    CPU_ENCODERS,
    GPU_ENCODERS,
    EncoderSettings,
    ProgressParser,
    choose_video_encoder,
    cpu_encoder_available,
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
) -> tuple[str, list[str]]:
    """Encode to ``output`` and return ``(codec, warnings)``.

    ``build_args(encoder, partial_output)`` returns the FFmpeg command. NVENC is used only
    when a test encode proved it works (see ``choose_video_encoder``); if the real encode
    still fails with NVENC it is retried once with libx264. FFmpeg writes to a ``.part``
    file that replaces ``output`` only after success, and is removed on failure or cancel.
    """
    codec, reason = choose_video_encoder(preference, ffmpeg, family)
    warnings = [] if codec == GPU_ENCODERS[family] or preference in ("libx264", "cpu") else [reason]
    low, high = progress_range
    partial = partial_path(output)

    def run(selected: str):
        parser = ProgressParser(duration)

        def on_line(line: str) -> None:
            fraction = parser.feed(line)
            if fraction is not None:
                ctx.report_progress(low + (high - low) * min(fraction, 0.999))

        args = build_args(EncoderSettings(codec=selected, quality=quality, bitrate_kbps=bitrate_kbps), str(partial))
        return run_streaming(args, on_line, cancel_event=ctx.cancel_event, cwd=str(workdir))

    try:
        result = run(codec)
        cpu = CPU_ENCODERS[family]
        if result.returncode != 0 and codec == GPU_ENCODERS[family] and cpu_encoder_available(ffmpeg, family):
            warnings.append(f"NVENC encoding failed; retried with the CPU encoder ({cpu}): "
                            + ffmpeg_error(result.stderr, "unknown error"))
            log.warning("NVENC encode failed, retrying on the CPU", extra={"job_id": ctx.job.id, "encoder": cpu})
            codec = cpu
            result = run(codec)
        if result.returncode != 0:
            raise StudioError(f"Export failed: {ffmpeg_error(result.stderr, 'unknown FFmpeg error')}")
        os.replace(partial, output)
    finally:
        # A cancelled FFmpeg can keep the file locked on Windows for a few seconds after it was
        # killed (an antivirus scan of the new file can add to that), so keep trying for up to 10 s.
        if partial.exists() and not remove_file(partial, attempts=100):
            log.warning("Could not remove partial export", extra={"path": str(partial)})
    return codec, warnings

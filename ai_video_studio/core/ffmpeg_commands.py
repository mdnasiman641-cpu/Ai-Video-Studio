"""FFmpeg command construction, progress parsing and encoder selection."""

from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from ai_video_studio.core.process import run_process

log = logging.getLogger(__name__)

VIDEO_ENCODERS = ("auto", "h264_nvenc", "libx264")


def audio_extract_args(ffmpeg: Path | str, source: str, output_wav: str) -> list[str]:
    """16 kHz mono 16-bit WAV: the input format Whisper models expect."""
    return [
        str(ffmpeg), "-hide_banner", "-nostdin", "-y",
        "-i", str(source),
        "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        "-progress", "pipe:1", "-nostats",
        str(output_wav),
    ]


@dataclass(frozen=True)
class EncoderSettings:
    codec: str = "libx264"   # libx264 | h264_nvenc
    quality: int = 20        # CRF (libx264) or CQ (NVENC); lower = better quality, larger file
    audio_bitrate: str = "192k"

    def video_args(self) -> list[str]:
        if self.codec == "h264_nvenc":
            return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr",
                    "-cq", str(self.quality), "-b:v", "0", "-pix_fmt", "yuv420p"]
        if self.codec == "libx264":
            return ["-c:v", "libx264", "-preset", "medium", "-crf", str(self.quality), "-pix_fmt", "yuv420p"]
        raise ValueError(f"Unsupported video encoder {self.codec!r}")


def burn_in_args(
    ffmpeg: Path | str,
    source: str,
    subtitle_filename: str,
    output: str,
    encoder: EncoderSettings,
    source_audio_codec: str | None,
    has_audio: bool,
) -> list[str]:
    """Re-encode ``source`` with subtitles rendered by libass.

    ``subtitle_filename`` must be a bare file name in the process working directory: this
    avoids FFmpeg filter-argument escaping of Windows paths (``C:\\...``), which is a
    common source of failures.
    """
    if any(ch in subtitle_filename for ch in "/\\:'[],;="):
        raise ValueError("subtitle_filename must be a plain file name")
    args = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-y",
        "-i", str(source),
        "-map", "0:v:0",
    ]
    if has_audio:
        args += ["-map", "0:a:0?"]
    args += ["-vf", f"subtitles={subtitle_filename}"]
    args += encoder.video_args()
    if has_audio:
        if source_audio_codec == "aac":
            args += ["-c:a", "copy"]
        else:
            args += ["-c:a", "aac", "-b:a", encoder.audio_bitrate]
    args += ["-map_metadata", "0", "-movflags", "+faststart",
             "-progress", "pipe:1", "-nostats", str(output)]
    return args


def frame_preview_args(ffmpeg: Path | str, source: str, subtitle_filename: str, at_seconds: float,
                       output_png: str) -> list[str]:
    """Render one frame with burned-in subtitles (exact libass rendering) to PNG.

    The seek is placed after ``-i`` so the subtitle filter sees the real timestamps.
    """
    if any(ch in subtitle_filename for ch in "/\\:'[],;="):
        raise ValueError("subtitle_filename must be a plain file name")
    return [
        str(ffmpeg), "-hide_banner", "-nostdin", "-y",
        "-i", str(source),
        "-ss", f"{max(0.0, at_seconds):.3f}",
        "-vf", f"subtitles={subtitle_filename}",
        "-frames:v", "1", str(output_png),
    ]


class ProgressParser:
    """Turns ``-progress pipe:1`` key=value lines into a 0..1 fraction."""

    def __init__(self, duration: float | None):
        self.duration = duration if duration and duration > 0 else None
        self.position = 0.0
        self.finished = False

    def feed(self, line: str) -> float | None:
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key in ("out_time_us", "out_time_ms"):  # both are microseconds in FFmpeg
            try:
                self.position = max(self.position, int(value) / 1_000_000)
            except ValueError:
                return None
            if self.duration:
                return min(1.0, self.position / self.duration)
        elif key == "progress" and value == "end":
            self.finished = True
            return 1.0
        return None


_encoder_cache: dict[str, bool] = {}
_cache_lock = threading.Lock()


def available_encoders(ffmpeg: Path | str) -> set[str]:
    result = run_process([str(ffmpeg), "-hide_banner", "-encoders"], timeout=20)
    names = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return names


def nvenc_usable(ffmpeg: Path | str) -> bool:
    """True if this FFmpeg build has h264_nvenc AND a short test encode succeeds.

    Being listed in ``-encoders`` only means support was compiled in; the test encode
    proves a compatible NVIDIA GPU and driver are present. The result is cached.
    """
    key = str(ffmpeg)
    with _cache_lock:
        if key in _encoder_cache:
            return _encoder_cache[key]
    ok = False
    try:
        if "h264_nvenc" in available_encoders(ffmpeg):
            test = run_process([str(ffmpeg), "-hide_banner", "-nostdin", "-f", "lavfi",
                                "-i", "color=c=black:s=256x256:d=0.2", "-c:v", "h264_nvenc",
                                "-f", "null", "-"], timeout=30)
            ok = test.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    log.info("NVENC availability checked", extra={"ffmpeg": key, "usable": ok})
    with _cache_lock:
        _encoder_cache[key] = ok
    return ok


def choose_video_encoder(preference: str, ffmpeg: Path | str) -> tuple[str, str]:
    """Return ``(codec, reason)`` for the user's preference (auto | h264_nvenc | libx264)."""
    if preference == "libx264":
        return "libx264", "CPU encoder selected in Settings"
    if nvenc_usable(ffmpeg):
        return "h264_nvenc", "NVIDIA NVENC"
    if preference == "h264_nvenc":
        return "libx264", "NVENC was requested but is not usable on this system; using CPU (libx264)"
    return "libx264", "NVENC not available; using CPU (libx264)"

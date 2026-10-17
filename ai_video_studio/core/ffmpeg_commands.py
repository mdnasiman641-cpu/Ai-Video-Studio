"""FFmpeg command construction, progress parsing and encoder selection."""

from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from ai_video_studio.core.errors import StudioError
from ai_video_studio.core.process import run_process

log = logging.getLogger(__name__)

VIDEO_ENCODERS = ("auto", "h264_nvenc", "libx264")


class EncoderUnavailableError(StudioError):
    """The requested codec cannot be encoded with this FFmpeg build and hardware."""


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


CPU_ENCODERS = {"h264": "libx264", "h265": "libx265"}
GPU_ENCODERS = {"h264": "h264_nvenc", "h265": "hevc_nvenc"}
# ``EncoderSettings.quality`` uses the libx264 CRF scale. H.265 reaches similar visual quality
# at a higher CRF/CQ value, so it is offset to keep the quality presets comparable.
QUALITY_OFFSET = {"libx264": 0, "h264_nvenc": 0, "libx265": 4, "hevc_nvenc": 4}


@dataclass(frozen=True)
class EncoderSettings:
    codec: str = "libx264"   # libx264 | h264_nvenc | libx265 | hevc_nvenc
    quality: int = 20        # CRF (libx264 scale) or CQ; lower = better quality, larger file
    audio_bitrate: str = "192k"
    bitrate_kbps: int | None = None  # target bitrate instead of constant quality

    def video_args(self) -> list[str]:
        if self.codec not in QUALITY_OFFSET:
            raise ValueError(f"Unsupported video encoder {self.codec!r}")
        q = str(self.quality + QUALITY_OFFSET[self.codec])
        rate = []
        if self.bitrate_kbps:
            b = int(self.bitrate_kbps)
            rate = ["-b:v", f"{b}k", "-maxrate", f"{int(b * 1.5)}k", "-bufsize", f"{b * 2}k"]
        if self.codec in ("h264_nvenc", "hevc_nvenc"):
            args = ["-c:v", self.codec, "-preset", "p5", "-tune", "hq", "-rc", "vbr"]
            args += rate or ["-cq", q, "-b:v", "0"]
        else:
            args = ["-c:v", self.codec, "-preset", "medium"] + (rate or ["-crf", q])
            if self.codec == "libx265":
                args += ["-x265-params", "log-level=error"]
        args += ["-pix_fmt", "yuv420p"]
        if self.codec in ("libx265", "hevc_nvenc"):
            args += ["-tag:v", "hvc1"]  # lets Apple players and editors recognise H.265 in MP4
        return args


UNSAFE_FILTER_FILENAME = "/\\:'[],;="


def check_filter_filename(name: str) -> None:
    """File names inside filter arguments must be plain names in the working directory.

    This avoids FFmpeg filter-argument escaping of Windows paths (``C:\\...``), which is a
    common source of failures.
    """
    if not name or any(ch in name for ch in UNSAFE_FILTER_FILENAME):
        raise ValueError("filter file names must be plain file names")



def subtitle_filter(subtitle_filename: str) -> str:
    """The libass filter that burns in an ASS file.

    The ``ass`` filter is used with complex text shaping (HarfBuzz): the ``subtitles`` filter
    uses libass's simple shaping, which leaves Bengali conjuncts (যুক্তাক্ষর) unjoined and vowel
    signs (কার) in the wrong place.
    """
    check_filter_filename(subtitle_filename)
    return f"ass={subtitle_filename}:shaping=complex"

def encode_args(
    ffmpeg: Path | str,
    source: str,
    video_filter: str,
    output: str,
    encoder: EncoderSettings,
    source_audio_codec: str | None,
    has_audio: bool,
) -> list[str]:
    """Re-encode the first video stream of ``source`` through ``video_filter``; keep the first audio stream.

    AAC audio is copied unchanged; other audio is converted to AAC. Timestamps are kept,
    so the frame rate and audio/video synchronisation of the source are preserved.
    """
    args = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-y",
        "-i", str(source),
        "-map", "0:v:0",
    ]
    if has_audio:
        args += ["-map", "0:a:0?"]
    args += ["-vf", video_filter]
    args += encoder.video_args()
    if has_audio:
        if source_audio_codec == "aac":
            args += ["-c:a", "copy"]
        else:
            args += ["-c:a", "aac", "-b:a", encoder.audio_bitrate]
    args += ["-map_metadata", "0", "-movflags", "+faststart",
             "-progress", "pipe:1", "-nostats", str(output)]
    return args


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

    ``subtitle_filename`` must be a bare file name in the process working directory
    (see :func:`check_filter_filename`).
    """
    check_filter_filename(subtitle_filename)
    return encode_args(ffmpeg, source, subtitle_filter(subtitle_filename), output, encoder,
                       source_audio_codec, has_audio)


def frame_preview_args(ffmpeg: Path | str, source: str, subtitle_filename: str, at_seconds: float,
                       output_png: str) -> list[str]:
    """Render one frame with burned-in subtitles (exact libass rendering) to PNG.

    The seek is placed after ``-i`` so the subtitle filter sees the real timestamps.
    """
    check_filter_filename(subtitle_filename)
    return [
        str(ffmpeg), "-hide_banner", "-nostdin", "-y",
        "-i", str(source),
        "-ss", f"{max(0.0, at_seconds):.3f}",
        "-vf", subtitle_filter(subtitle_filename),
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


def nvenc_usable(ffmpeg: Path | str, encoder: str = "h264_nvenc") -> bool:
    """True if this FFmpeg build has ``encoder`` (an NVENC encoder) AND a short test encode succeeds.

    Being listed in ``-encoders`` only means support was compiled in; the test encode
    proves a compatible NVIDIA GPU and driver are present. The result is cached.
    """
    key = f"{ffmpeg}|{encoder}"
    with _cache_lock:
        if key in _encoder_cache:
            return _encoder_cache[key]
    ok = False
    try:
        if encoder in available_encoders(ffmpeg):
            test = run_process([str(ffmpeg), "-hide_banner", "-nostdin", "-f", "lavfi",
                                "-i", "color=c=black:s=256x256:d=0.2", "-c:v", encoder,
                                "-f", "null", "-"], timeout=30)
            ok = test.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    log.info("NVENC availability checked", extra={"ffmpeg": str(ffmpeg), "encoder": encoder, "usable": ok})
    with _cache_lock:
        _encoder_cache[key] = ok
    return ok


def _gpu_usable(ffmpeg: Path | str, family: str) -> bool:
    return nvenc_usable(ffmpeg) if family == "h264" else nvenc_usable(ffmpeg, GPU_ENCODERS[family])


def cpu_encoder_available(ffmpeg: Path | str, family: str) -> bool:
    if family == "h264":
        return True  # libx264 is required by the app (caption burn-in, reframe export)
    try:
        return CPU_ENCODERS[family] in available_encoders(ffmpeg)
    except (OSError, subprocess.TimeoutExpired):
        return False


def choose_video_encoder(preference: str, ffmpeg: Path | str, family: str = "h264") -> tuple[str, str]:
    """Return ``(encoder, reason)``.

    ``preference`` is auto | gpu | cpu (``h264_nvenc`` / ``libx264`` are accepted as the
    older names for gpu / cpu); ``family`` is h264 | h265. NVENC is only used when a test
    encode proved it works. Raises ``EncoderUnavailableError`` when the codec cannot be
    encoded at all (H.265 without libx265 and without a usable NVENC).
    """
    if family not in CPU_ENCODERS:
        raise ValueError(f"Unknown codec {family!r}")
    cpu, gpu = CPU_ENCODERS[family], GPU_ENCODERS[family]
    label = "H.264" if family == "h264" else "H.265"
    if preference in ("libx264", "cpu"):
        if cpu_encoder_available(ffmpeg, family):
            return cpu, "CPU encoder selected in Settings"
        if _gpu_usable(ffmpeg, family):
            return gpu, f"{cpu} is not in this FFmpeg build; using NVIDIA NVENC"
        raise EncoderUnavailableError(f"{label} cannot be encoded: this FFmpeg build has no {cpu} and NVENC "
                                      f"is not usable. Choose H.264 or use an FFmpeg build with {cpu}.")
    if _gpu_usable(ffmpeg, family):
        return gpu, "NVIDIA NVENC"
    if not cpu_encoder_available(ffmpeg, family):
        raise EncoderUnavailableError(f"{label} cannot be encoded: NVENC is not usable and this FFmpeg build "
                                      f"has no {cpu}. Choose H.264.")
    if preference in ("h264_nvenc", "gpu"):
        return cpu, f"NVENC was requested but is not usable on this system; using CPU ({cpu})"
    return cpu, f"NVENC not available; using CPU ({cpu})"

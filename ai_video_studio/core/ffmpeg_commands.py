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

VIDEO_ENCODERS = ("auto", "h264_nvenc", "libx264")  # the Settings page's default encoder choice


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
# hardware encoder family -> encoder per codec
HW_ENCODERS = {"nvenc": GPU_ENCODERS, "qsv": {"h264": "h264_qsv", "h265": "hevc_qsv"},
               "amf": {"h264": "h264_amf", "h265": "hevc_amf"}}
HW_LABELS = {"nvenc": "NVIDIA NVENC", "qsv": "Intel Quick Sync", "amf": "AMD AMF", "cpu": "CPU"}
AUTO_ORDER = ("nvenc", "qsv", "amf")  # Auto tries these in order (a preference, not a speed promise)
# ``EncoderSettings.quality`` uses the libx264 CRF scale. H.265 reaches similar visual quality
# at a higher CRF/CQ value, so it is offset to keep the quality presets comparable.
QUALITY_OFFSET = {"libx264": 0, "h264_nvenc": 0, "h264_qsv": 0, "h264_amf": 0,
                  "libx265": 4, "hevc_nvenc": 4, "hevc_qsv": 4, "hevc_amf": 4}
# Encoder speed settings per export mode (render.output.MODES); "custom" uses the CPU preset chosen
# by the user and the Standard settings for hardware encoders.
CPU_SPEED = {"draft": "ultrafast", "fast": "veryfast", "standard": "medium", "high": "slow", "maximum": "slower"}
NVENC_SPEED = {"draft": ("p1", "ll", None), "fast": ("p3", "hq", None), "standard": ("p5", "hq", None),
               "high": ("p6", "hq", "qres"), "maximum": ("p7", "hq", "fullres")}
QSV_SPEED = {"draft": "veryfast", "fast": "faster", "standard": "medium", "high": "slower", "maximum": "veryslow"}
AMF_SPEED = {"draft": "speed", "fast": "speed", "standard": "balanced", "high": "quality", "maximum": "quality"}


def encoder_family(codec: str) -> str:
    if codec.endswith("_nvenc"):
        return "nvenc"
    if codec.endswith("_qsv"):
        return "qsv"
    if codec.endswith("_amf"):
        return "amf"
    return "cpu"


def encoder_label(codec: str) -> str:
    """What the user sees, e.g. "NVIDIA NVENC H.264" or "CPU (libx264)"."""
    family = encoder_family(codec)
    if family == "cpu":
        return f"CPU ({codec})"
    return f"{HW_LABELS[family]} {'H.265' if codec.startswith('hevc') else 'H.264'}"


@dataclass(frozen=True)
class EncoderSettings:
    codec: str = "libx264"   # libx264 | libx265 | h264/hevc _nvenc | _qsv | _amf
    quality: int = 20        # CRF (libx264 scale) or CQ; lower = better quality, larger file
    audio_bitrate: str = "192k"
    bitrate_kbps: int | None = None  # target bitrate instead of constant quality
    speed: str = "standard"  # export mode: draft | fast | standard | high | maximum | custom
    cpu_preset: str = "medium"  # x264/x265 preset for the custom mode

    def video_args(self) -> list[str]:
        if self.codec not in QUALITY_OFFSET:
            raise ValueError(f"Unsupported video encoder {self.codec!r}")
        q = str(self.quality + QUALITY_OFFSET[self.codec])
        speed = self.speed if self.speed in CPU_SPEED else "standard"
        rate = []
        if self.bitrate_kbps:
            b = int(self.bitrate_kbps)
            rate = ["-b:v", f"{b}k", "-maxrate", f"{int(b * 1.5)}k", "-bufsize", f"{b * 2}k"]
        family = encoder_family(self.codec)
        pix = "yuv420p"
        if family == "nvenc":
            preset, tune, multipass = NVENC_SPEED[speed]
            args = ["-c:v", self.codec, "-preset", preset, "-tune", tune, "-rc", "vbr"]
            if multipass:
                args += ["-multipass", multipass]
            if speed == "maximum":
                args += ["-spatial-aq", "1"]
            args += rate or ["-cq", q, "-b:v", "0"]
        elif family == "qsv":
            args = ["-c:v", self.codec, "-preset", QSV_SPEED[speed]] + (rate or ["-global_quality", q])
            pix = "nv12"
        elif family == "amf":
            args = ["-c:v", self.codec, "-quality", AMF_SPEED[speed]]
            args += (["-rc", "vbr_peak"] + rate) if rate else ["-rc", "cqp", "-qp_i", q, "-qp_p", q, "-qp_b", q]
            pix = "nv12"
        else:
            preset = self.cpu_preset if self.speed == "custom" else CPU_SPEED[speed]
            args = ["-c:v", self.codec, "-preset", preset] + (rate or ["-crf", q])
            if self.codec == "libx265":
                args += ["-x265-params", "log-level=error"]
        args += ["-pix_fmt", pix]
        if self.codec.startswith("hevc") or self.codec == "libx265":
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
        self.speed: float | None = None   # FFmpeg's speed (× realtime), from ``speed=1.5x``
        self.fps: float | None = None     # frames encoded per second

    def feed(self, line: str) -> float | None:
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key == "speed":
            try:
                self.speed = float(value.rstrip("x")) if value not in ("N/A", "") else self.speed
            except ValueError:
                pass
            return None
        if key == "fps":
            try:
                self.fps = float(value)
            except ValueError:
                pass
            return None
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


def _test_encode(ffmpeg: Path | str, encoder: str) -> bool:
    pix = ["-pix_fmt", "nv12"] if encoder.endswith(("_qsv", "_amf")) else []
    test = run_process([str(ffmpeg), "-hide_banner", "-nostdin", "-f", "lavfi",
                        "-i", "color=c=black:s=256x256:d=0.2"] + pix + ["-c:v", encoder, "-f", "null", "-"], timeout=30)
    if test.returncode != 0:
        lines = [ln.strip() for ln in test.stderr.splitlines() if ln.strip()]
        _encoder_errors[f"{ffmpeg}|{encoder}"] = lines[-1] if lines else f"exit code {test.returncode}"
    return test.returncode == 0


_encoder_errors: dict[str, str] = {}


def nvenc_usable(ffmpeg: Path | str, encoder: str = "h264_nvenc") -> bool:
    """True if this FFmpeg build has ``encoder`` (a hardware encoder) AND a short test encode succeeds.

    Being listed in ``-encoders`` only means support was compiled in; the test encode
    proves a compatible GPU and driver are present. The result is cached. (Named for NVENC,
    which it was written for; it checks Quick Sync and AMF encoders the same way.)
    """
    key = f"{ffmpeg}|{encoder}"
    with _cache_lock:
        if key in _encoder_cache:
            return _encoder_cache[key]
    ok = False
    try:
        if encoder in available_encoders(ffmpeg):
            ok = _test_encode(ffmpeg, encoder)
        else:
            _encoder_errors[key] = "not included in this FFmpeg build"
    except (OSError, subprocess.TimeoutExpired) as exc:
        _encoder_errors[key] = f"the test encode did not finish ({exc})"
        ok = False
    log.info("Hardware encoder checked", extra={"ffmpeg": str(ffmpeg), "encoder": encoder, "usable": ok})
    with _cache_lock:
        _encoder_cache[key] = ok
    return ok


def hw_encoder_usable(ffmpeg: Path | str, family: str, codec_family: str = "h264") -> bool:
    return nvenc_usable(ffmpeg, HW_ENCODERS[family][codec_family])


def encoder_error(ffmpeg: Path | str, encoder: str) -> str:
    """Why a hardware encoder is not usable (the FFmpeg message of its test encode)."""
    return _encoder_errors.get(f"{ffmpeg}|{encoder}", "the test encode failed")


def _gpu_usable(ffmpeg: Path | str, family: str) -> bool:
    return nvenc_usable(ffmpeg) if family == "h264" else nvenc_usable(ffmpeg, GPU_ENCODERS[family])


def cpu_encoder_available(ffmpeg: Path | str, family: str) -> bool:
    if family == "h264":
        return True  # libx264 is required by the app (caption burn-in, reframe export)
    try:
        return CPU_ENCODERS[family] in available_encoders(ffmpeg)
    except (OSError, subprocess.TimeoutExpired):
        return False


def normalise_preference(preference: str) -> str:
    return {"gpu": "nvenc", "h264_nvenc": "nvenc", "libx264": "cpu"}.get(preference, preference)


def choose_video_encoder(preference: str, ffmpeg: Path | str, family: str = "h264",
                         allow_fallback: bool = True) -> tuple[str, str]:
    """Return ``(encoder, reason)``.

    ``preference`` is auto | nvenc | qsv | amf | cpu (``gpu``/``h264_nvenc``/``libx264`` are the
    older names); ``family`` is h264 | h265. A hardware encoder is only used when a test encode
    proved it works. Auto tries NVENC, Quick Sync, then AMF, then the CPU. A chosen hardware
    encoder that is not usable falls back to the CPU only with ``allow_fallback``; otherwise, or
    when the codec cannot be encoded at all, ``EncoderUnavailableError`` is raised.
    """
    if family not in CPU_ENCODERS:
        raise ValueError(f"Unknown codec {family!r}")
    preference = normalise_preference(preference)
    cpu = CPU_ENCODERS[family]
    label = "H.264" if family == "h264" else "H.265"
    if preference == "cpu":
        if cpu_encoder_available(ffmpeg, family):
            return cpu, "CPU encoder selected"
        for hw in AUTO_ORDER:
            if hw_encoder_usable(ffmpeg, hw, family):
                return HW_ENCODERS[hw][family], f"{cpu} is not in this FFmpeg build; using {HW_LABELS[hw]}"
        raise EncoderUnavailableError(f"{label} cannot be encoded: this FFmpeg build has no {cpu} and no hardware "
                                      f"encoder is usable. Choose H.264 or use an FFmpeg build with {cpu}.")
    if preference in HW_ENCODERS:
        wanted = HW_ENCODERS[preference][family]
        if hw_encoder_usable(ffmpeg, preference, family):
            return wanted, HW_LABELS[preference]
        why = f"{HW_LABELS[preference]} ({wanted}) is not usable on this system: {encoder_error(ffmpeg, wanted)}"
        if not allow_fallback:
            raise EncoderUnavailableError(why + ". Choose another encoder, or allow the CPU fallback.")
        if not cpu_encoder_available(ffmpeg, family):
            raise EncoderUnavailableError(why + f", and this FFmpeg build has no {cpu}. Choose H.264.")
        return cpu, why + f"; using CPU ({cpu})"
    for hw in AUTO_ORDER:
        if hw_encoder_usable(ffmpeg, hw, family):
            return HW_ENCODERS[hw][family], HW_LABELS[hw]
    if not cpu_encoder_available(ffmpeg, family):
        raise EncoderUnavailableError(f"{label} cannot be encoded: no hardware encoder is usable and this FFmpeg "
                                      f"build has no {cpu}. Choose H.264.")
    return cpu, f"No hardware encoder is usable (NVENC not available); using CPU ({cpu})"

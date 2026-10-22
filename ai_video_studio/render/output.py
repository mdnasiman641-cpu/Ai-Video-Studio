"""Output (encode) settings chosen by the user: export mode, codec, encoder, quality, frame rate,
container and audio.

**Export modes.** Five fixed modes trade speed for quality with genuinely different settings per
encoder (see :data:`MODES` and ``core.ffmpeg_commands.encoder_args``); Custom exposes every value.

=========  =====  ==============  ===============  ===============  ========  =====
mode       CRF*   x264 / x265     NVENC            Quick Sync       AMF       audio
=========  =====  ==============  ===============  ===============  ========  =====
draft      30     ultrafast       p1, ll           veryfast         speed     96k
fast       25     veryfast        p3, hq           faster           speed     128k
standard   21     medium          p5, hq           medium           balanced  160k
high       18     slow            p6, hq, 2-pass   slower           quality   192k
maximum    16     slower          p7, hq, 2-pass   veryslow         quality   256k
=========  =====  ==============  ===============  ===============  ========  =====

* CRF on the x264 scale (CQ for NVENC, global_quality for Quick Sync, QP for AMF); H.265 adds 4.
Settings stored before export modes existed have no ``mode`` and load as Custom, so they keep
encoding exactly as before.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

from ai_video_studio.core.errors import StudioError

CODECS = {"h264": "H.264 (most compatible)", "h265": "H.265 / HEVC (smaller files)"}
ENCODER_PREFERENCES = {"auto": "Auto (best tested encoder)", "nvenc": "NVIDIA NVENC", "qsv": "Intel Quick Sync",
                       "amf": "AMD AMF", "cpu": "CPU software (x264 / x265)"}
LEGACY_ENCODERS = {"gpu": "nvenc", "h264_nvenc": "nvenc", "libx264": "cpu"}
MODES = {
    "draft": "Draft / Fast Preview",
    "fast": "Fast Export",
    "standard": "Standard / Balanced (recommended)",
    "high": "High Quality",
    "maximum": "Maximum Quality (slow)",
    "custom": "Custom",
}
MODE_NOTES = {
    "draft": "Quickest, visibly lower quality: for checking framing, captions, cuts and timing. Not for delivery.",
    "fast": "Fast with acceptable quality; uses the GPU encoder when one is available.",
    "standard": "Balanced quality, file size and speed. Recommended for everyday exports.",
    "high": "High-quality final output with little compression; slower than Standard.",
    "maximum": "Most detail and the largest files; rendering takes noticeably longer.",
    "custom": "Every setting below is yours to choose.",
}
MODE_CRF = {"draft": 30, "fast": 25, "standard": 21, "high": 18, "maximum": 16}
MODE_AUDIO = {"draft": 96, "fast": 128, "standard": 160, "high": 192, "maximum": 256}
# CRF / CQ on the libx264 scale (H.265 is offset in EncoderSettings); lower = better and larger.
QUALITY_PRESETS = {"high": 18, "balanced": 21, "small": 26}
QUALITY_LABELS = {"high": "High quality", "balanced": "Balanced", "small": "Smaller file", "custom": "Custom CRF/CQ"}
CPU_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
RATE_CONTROLS = {"quality": "Constant quality", "bitrate": "Target bitrate"}
FRAME_RATES = ("original", "24", "25", "30", "50", "60")
CONTAINERS = {"mp4": "MP4", "mov": "MOV", "mkv": "MKV"}
AUDIO_MODES = {
    "auto": "Keep AAC audio unchanged, convert other audio to AAC",
    "aac": "Re-encode to AAC",
    "none": "No audio",
}
AUDIO_BITRATES = (96, 128, 160, 192, 256, 320)
HW_DECODE = {"off": "Off (CPU decoding)", "auto": "Auto (GPU decoding when FFmpeg can, else CPU)"}


class OutputSettingsError(StudioError):
    pass


@dataclass
class OutputSettings:
    codec: str = "h264"
    encoder: str = "auto"
    quality: str = "balanced"
    crf: int = 21                   # used when quality == "custom"
    rate_control: str = "quality"
    bitrate_kbps: int = 8000        # used when rate_control == "bitrate"
    fps: str = "original"
    audio: str = "auto"
    audio_bitrate: int = 192
    all_audio_tracks: bool = False  # keep every audio track (not possible together with cuts)
    mode: str = "standard"          # export mode (MODES); "custom" uses the fields above
    allow_fallback: bool = True     # a chosen hardware encoder may fall back to the CPU (Auto always may)
    cpu_preset: str = "medium"      # Custom: x264/x265 speed preset
    container: str = "mp4"
    hw_decode: str = "off"          # off | auto (-hwaccel auto)

    def validate(self) -> None:
        errors = []
        if self.mode not in MODES:
            errors.append(f"Unknown export mode '{self.mode}'")
        if self.cpu_preset not in CPU_PRESETS:
            errors.append(f"Unknown CPU preset '{self.cpu_preset}'")
        if self.container not in CONTAINERS:
            errors.append(f"Unsupported container '{self.container}'")
        if self.hw_decode not in HW_DECODE:
            errors.append(f"Unknown hardware decoding option '{self.hw_decode}'")
        if self.codec not in CODECS:
            errors.append(f"Unknown codec '{self.codec}'")
        if self.encoder not in ENCODER_PREFERENCES:
            errors.append(f"Unknown encoder '{self.encoder}'")
        if self.quality not in QUALITY_LABELS:
            errors.append(f"Unknown quality preset '{self.quality}'")
        if not 10 <= int(self.crf) <= 40:
            errors.append("CRF/CQ must be between 10 and 40")
        if self.rate_control not in RATE_CONTROLS:
            errors.append(f"Unknown rate control '{self.rate_control}'")
        if not 500 <= int(self.bitrate_kbps) <= 100_000:
            errors.append("Bitrate must be between 500 and 100000 kbit/s")
        if self.fps not in FRAME_RATES:
            errors.append(f"Unsupported frame rate '{self.fps}'")
        if self.audio not in AUDIO_MODES:
            errors.append(f"Unknown audio mode '{self.audio}'")
        if int(self.audio_bitrate) not in AUDIO_BITRATES:
            errors.append(f"Audio bitrate must be one of {', '.join(map(str, AUDIO_BITRATES))} kbit/s")
        if errors:
            raise OutputSettingsError("; ".join(errors))

    @property
    def crf_value(self) -> int:
        if self.mode != "custom":
            return MODE_CRF[self.mode]
        return int(self.crf) if self.quality == "custom" else QUALITY_PRESETS[self.quality]

    @property
    def target_bitrate(self) -> int | None:
        return int(self.bitrate_kbps) if self.mode == "custom" and self.rate_control == "bitrate" else None

    @property
    def audio_kbps(self) -> int:
        return int(self.audio_bitrate) if self.mode == "custom" else MODE_AUDIO[self.mode]

    @property
    def speed(self) -> str:
        """The encoder speed/quality level (``core.ffmpeg_commands.encoder_args``)."""
        return self.mode

    @property
    def suffix(self) -> str:
        return "." + self.container

    def describe(self) -> str:
        """Short description for summaries and the queue."""
        if self.mode != "custom":
            return MODES[self.mode].split(" (")[0]
        rate = f"{self.bitrate_kbps} kbit/s" if self.rate_control == "bitrate" else f"CRF/CQ {self.crf_value}"
        return f"Custom ({rate}, {self.cpu_preset})"

    def target_fps(self) -> float | None:
        """The output frame rate, or None to keep the source's timing."""
        return None if self.fps == "original" else float(self.fps)

    def notes(self, source_fps: float | None) -> list[str]:
        """Things the user should know about these settings for a given source."""
        notes = []
        target = self.target_fps()
        if target and source_fps and target > source_fps * 1.01:
            notes.append(f"{target:g} fps is higher than the source ({source_fps:.3g} fps): frames are repeated, "
                         "motion does not become smoother.")
        return notes

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "OutputSettings":
        """Read stored settings. Older settings (no ``mode``) load as Custom and encode as before;
        the older encoder names ``gpu``/``h264_nvenc``/``libx264`` mean NVENC/CPU."""
        known = {f.name: f.type for f in fields(cls)}
        values = {k: v for k, v in (data or {}).items() if k in known}
        if data and "mode" not in data:
            values["mode"] = "custom"
        if values.get("encoder") in LEGACY_ENCODERS:
            values["encoder"] = LEGACY_ENCODERS[values["encoder"]]
        return cls(**values)

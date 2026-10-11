"""Output (encode) settings chosen by the user: codec, encoder, quality, frame rate and audio."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

from ai_video_studio.core.errors import StudioError

CODECS = {"h264": "H.264 (most compatible)", "h265": "H.265 / HEVC (smaller files)"}
ENCODER_PREFERENCES = {"auto": "Automatic (NVENC if usable, else CPU)", "gpu": "NVIDIA NVENC (GPU)",
                       "cpu": "CPU (x264 / x265)"}
# CRF / CQ on the libx264 scale (H.265 is offset in EncoderSettings); lower = better and larger.
QUALITY_PRESETS = {"high": 18, "balanced": 21, "small": 26}
QUALITY_LABELS = {"high": "High quality", "balanced": "Balanced", "small": "Smaller file", "custom": "Custom CRF/CQ"}
RATE_CONTROLS = {"quality": "Constant quality", "bitrate": "Target bitrate"}
FRAME_RATES = ("original", "24", "25", "30", "50", "60")
AUDIO_MODES = {
    "auto": "Keep AAC audio unchanged, convert other audio to AAC",
    "aac": "Re-encode to AAC",
    "none": "No audio",
}
AUDIO_BITRATES = (96, 128, 160, 192, 256, 320)


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

    def validate(self) -> None:
        errors = []
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
        return int(self.crf) if self.quality == "custom" else QUALITY_PRESETS[self.quality]

    @property
    def target_bitrate(self) -> int | None:
        return int(self.bitrate_kbps) if self.rate_control == "bitrate" else None

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
        known = {f.name: f.type for f in fields(cls)}
        values = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**values)

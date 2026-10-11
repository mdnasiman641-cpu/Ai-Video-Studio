"""Validated application settings stored in SQLite."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ai_video_studio.core.errors import SettingsError, StudioError
from ai_video_studio.core.paths import default_projects_dir
from ai_video_studio.storage.database import Database


@dataclass(frozen=True)
class SettingSpec:
    type: type
    default: Any
    label: str
    validate: Callable[[Any], str | None] = lambda v: None


def _int_range(low: int, high: int) -> Callable[[Any], str | None]:
    return lambda v: None if low <= v <= high else f"must be between {low} and {high}"


def _optional_dir(value: str) -> str | None:
    if value and not Path(value).is_dir():
        return "folder does not exist"
    return None


def _choice(*options: str) -> Callable[[Any], str | None]:
    return lambda v: None if v in options else f"must be one of {', '.join(options)}"


def _output_settings_json(value: str) -> str | None:
    if not value:
        return None
    try:
        from ai_video_studio.render.output import OutputSettings

        OutputSettings.from_dict(json.loads(value)).validate()
    except (ValueError, TypeError, StudioError) as exc:
        return f"invalid export settings ({exc})"
    return None


def _float_range(low: float, high: float) -> Callable[[Any], str | None]:
    return lambda v: None if low <= v <= high else f"must be between {low:g} and {high:g}"


# Auto reframe defaults for videos that have not been reframed yet (each video then keeps its
# own copy in the project). Ranges match reframe.model.ReframeSettings.validate().
_TRACKING_MODES = ("auto_face", "active_speaker", "speech_audio", "manual", "auto_visible", "center")


SETTINGS: dict[str, SettingSpec] = {
    "ffmpeg_dir": SettingSpec(str, "", "FFmpeg folder", _optional_dir),
    "projects_dir": SettingSpec(str, str(default_projects_dir()), "Default projects folder"),
    # Background jobs that may run at once. Kept low by default: one 1080p NVENC/x264
    # render already uses a large share of CPU/GPU.
    "max_concurrent_jobs": SettingSpec(int, 2, "Maximum concurrent jobs", _int_range(1, 8)),
    # Video renders (exports) that may run at once, within the limit above. One 1080p render
    # already keeps the CPU or the NVENC encoder busy; more mainly costs memory.
    "max_concurrent_renders": SettingSpec(int, 1, "Maximum concurrent renders", _int_range(1, 4)),
    "log_level": SettingSpec(str, "INFO", "Log level", _choice("DEBUG", "INFO", "WARNING", "ERROR")),
    "reopen_last_project": SettingSpec(bool, True, "Reopen last project on startup"),
    "preview_volume": SettingSpec(int, 80, "Preview volume", _int_range(0, 100)),
    # Captions / transcription
    "whisper_model": SettingSpec(str, "small", "Whisper model",
                                 _choice("tiny", "base", "small", "medium", "large-v3", "large-v3-turbo")),
    "whisper_model_dir": SettingSpec(str, "", "Local Whisper model folder", _optional_dir),
    "transcription_device": SettingSpec(str, "auto", "Transcription device", _choice("auto", "cuda", "cpu")),
    "transcription_language": SettingSpec(str, "auto", "Default caption language"),
    "caption_max_chars": SettingSpec(int, 42, "Max characters per caption", _int_range(10, 120)),
    "caption_max_seconds": SettingSpec(int, 6, "Max seconds per caption", _int_range(1, 20)),
    # Export
    "video_encoder": SettingSpec(str, "auto", "Video encoder", _choice("auto", "h264_nvenc", "libx264")),
    "video_quality": SettingSpec(int, 20, "Video quality (CRF/CQ)", _int_range(10, 40)),
    # Last export settings used for edited exports (Smart Cut, Resize & Crop), as JSON
    "export_output_settings": SettingSpec(str, "", "Export settings", _output_settings_json),
    # AI providers (Phase 7) are stored as profiles (ai/profiles.py). The earlier settings
    # ai_correction_provider, gemini_model, openai_model and ai_timeout_seconds are read once,
    # directly from the database, to create the first profiles.
    # Auto reframe / tracking defaults
    "reframe_tracking_mode": SettingSpec(str, "auto_face", "Tracking mode", _choice(*_TRACKING_MODES)),
    "reframe_confidence_threshold": SettingSpec(float, 0.6, "Tracking confidence threshold", _float_range(0.3, 0.99)),
    "reframe_speech_threshold": SettingSpec(float, 0.5, "Speech-activity threshold", _float_range(0.1, 0.95)),
    "reframe_switch_seconds": SettingSpec(float, 1.0, "Speaker-switch confirmation (s)", _float_range(0.0, 5.0)),
    "reframe_speaker_hold_seconds": SettingSpec(float, 3.0, "Keep last speaker when uncertain (s)",
                                                _float_range(0.0, 15.0)),
    "reframe_hold_seconds": SettingSpec(float, 1.0, "Subject-lost hold (s)", _float_range(0.0, 10.0)),
    "reframe_smoothing": SettingSpec(float, 0.5, "Camera smoothing", _float_range(0.0, 1.0)),
    "reframe_padding": SettingSpec(float, 0.2, "Crop padding", _float_range(0.0, 1.0)),
    "reframe_max_upscale": SettingSpec(float, 2.0, "Maximum upscale factor", _float_range(1.0, 4.0)),
    "reframe_fallback": SettingSpec(str, "center", "Fallback", _choice("hold", "center", "group")),
    "reframe_analysis_fps": SettingSpec(float, 5.0, "Analysis frame rate", _float_range(0.5, 30.0)),
    "reframe_av_fps": SettingSpec(float, 15.0, "Mouth analysis frame rate", _float_range(5.0, 30.0)),
}

# Settings key -> ReframeSettings field
REFRAME_DEFAULT_KEYS = {
    "reframe_tracking_mode": "tracking_mode", "reframe_confidence_threshold": "confidence_threshold",
    "reframe_speech_threshold": "speech_threshold", "reframe_switch_seconds": "switch_seconds",
    "reframe_speaker_hold_seconds": "speaker_hold_seconds", "reframe_hold_seconds": "hold_seconds",
    "reframe_smoothing": "smoothing", "reframe_padding": "padding", "reframe_max_upscale": "max_upscale",
    "reframe_fallback": "fallback", "reframe_analysis_fps": "analysis_fps", "reframe_av_fps": "av_fps",
}


class SettingsStore:
    def __init__(self, db: Database):
        self._db = db
        self._listeners: list[Callable[[str, Any], None]] = []

    def get(self, key: str) -> Any:
        spec = self._spec(key)
        row = self._db.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            return spec.default
        try:
            value = json.loads(row["value"])
            return self._coerce(key, value)
        except (ValueError, SettingsError):
            return spec.default

    def set(self, key: str, value: Any) -> None:
        value = self._coerce(key, value)
        error = self._spec(key).validate(value)
        if error:
            raise SettingsError(f"{self._spec(key).label}: {error}")
        self._db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        for listener in list(self._listeners):
            listener(key, value)

    def update(self, values: dict[str, Any]) -> None:
        """Validate every value first, then store them all (all-or-nothing)."""
        coerced = {}
        errors = []
        for key, value in values.items():
            try:
                coerced[key] = self._coerce(key, value)
                err = self._spec(key).validate(coerced[key])
                if err:
                    errors.append(f"{self._spec(key).label}: {err}")
            except SettingsError as exc:
                errors.append(str(exc))
        if errors:
            raise SettingsError("\n".join(errors))
        for key, value in coerced.items():
            self.set(key, value)

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._db.execute("DELETE FROM settings")
            for name in SETTINGS:
                for listener in list(self._listeners):
                    listener(name, SETTINGS[name].default)
        else:
            self._spec(key)
            self._db.execute("DELETE FROM settings WHERE key = ?", (key,))
            for listener in list(self._listeners):
                listener(key, SETTINGS[key].default)

    def all(self) -> dict[str, Any]:
        return {key: self.get(key) for key in SETTINGS}

    def subscribe(self, listener: Callable[[str, Any], None]) -> None:
        self._listeners.append(listener)

    @staticmethod
    def _spec(key: str) -> SettingSpec:
        try:
            return SETTINGS[key]
        except KeyError:
            raise SettingsError(f"Unknown setting '{key}'") from None

    def _coerce(self, key: str, value: Any) -> Any:
        spec = self._spec(key)
        try:
            if spec.type is bool:
                if isinstance(value, str):
                    return value.strip().lower() in ("1", "true", "yes", "on")
                return bool(value)
            if spec.type is int:
                if isinstance(value, bool):
                    raise ValueError
                return int(value)
            if spec.type is str:
                return "" if value is None else str(value).strip()
            if spec.type is float:
                if isinstance(value, bool):
                    raise ValueError
                return round(float(value), 4)
            return spec.type(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{spec.label}: invalid value {value!r}") from None

"""Validated application settings stored in SQLite."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ai_video_studio.core.errors import SettingsError
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


def _non_empty(value: str) -> str | None:
    return None if value.strip() else "must not be empty"


def _choice(*options: str) -> Callable[[Any], str | None]:
    return lambda v: None if v in options else f"must be one of {', '.join(options)}"


SETTINGS: dict[str, SettingSpec] = {
    "ffmpeg_dir": SettingSpec(str, "", "FFmpeg folder", _optional_dir),
    "projects_dir": SettingSpec(str, str(default_projects_dir()), "Default projects folder"),
    # Background jobs that may run at once. Kept low by default: one 1080p NVENC/x264
    # render already uses a large share of CPU/GPU.
    "max_concurrent_jobs": SettingSpec(int, 2, "Maximum concurrent jobs", _int_range(1, 8)),
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
    # Optional AI caption correction (off unless a provider is chosen)
    "ai_correction_provider": SettingSpec(str, "none", "AI correction provider", _choice("none", "gemini", "openai")),
    "gemini_model": SettingSpec(str, "gemini-2.5-flash", "Gemini model", _non_empty),
    "openai_model": SettingSpec(str, "gpt-4.1-mini", "OpenAI model", _non_empty),
    "ai_timeout_seconds": SettingSpec(int, 60, "AI request timeout (s)", _int_range(10, 300)),
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
            return spec.type(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{spec.label}: invalid value {value!r}") from None

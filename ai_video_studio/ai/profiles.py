"""AI provider profiles: which services and models the user configured, and what they can do.

A profile holds no secret. Its API key lives in the Windows Credential Manager (account =
profile id) or in an environment variable the profile names. Profiles are stored in the user
database in routing order: the first enabled profile that can do a task is the primary, the
others are fallbacks.

Capabilities are explicit. AI Video Studio only ever sends **text** (caption text); audio and
video understanding are listed so the limits are visible, but the app never uploads media to a
cloud provider, so those capabilities cannot be enabled.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

from ai_video_studio.core.errors import StudioError
from ai_video_studio.core.media import now_iso
from ai_video_studio.storage.database import Database

PROVIDER_TYPES = {
    "gemini": "Google Gemini",
    "openai": "OpenAI",
    "openai_compatible": "OpenAI-compatible API (custom)",
}
DEFAULT_BASE_URLS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "openai": "https://api.openai.com/v1",
    "openai_compatible": "http://localhost:11434/v1",
}
CAPABILITIES = {
    "text": "Text generation",
    "json": "Structured JSON output (native JSON mode)",
    "caption_correction": "Caption correction",
    "titles": "Title and description generation",
    "audio": "Audio understanding",
    "video": "Video understanding",
}
MEDIA_CAPABILITIES = {"audio", "video"}
MEDIA_NOTE = "Not available: AI Video Studio never sends audio or video to cloud AI providers."
DEFAULT_CAPABILITIES = {
    "gemini": ["text", "json", "caption_correction", "titles"],
    "openai": ["text", "json", "caption_correction", "titles"],
    "openai_compatible": ["text", "caption_correction", "titles"],
}
DEFAULT_MODELS = {"gemini": "gemini-2.5-flash", "openai": "gpt-4.1-mini", "openai_compatible": ""}
BUILTIN_IDS = ("gemini", "openai")  # keys stored by earlier versions use these account names
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ProfileError(StudioError):
    pass


@dataclass
class ProviderProfile:
    display_name: str
    provider_type: str
    model: str
    base_url: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    enabled: bool = True
    capabilities: list[str] = field(default_factory=lambda: ["text", "caption_correction"])
    timeout: int = 60
    max_retries: int = 2
    rate_limit_per_minute: int = 0          # 0 = no local limit
    cost_input_per_million: float | None = None   # your price per million input tokens (optional)
    cost_output_per_million: float | None = None
    key_env: str = ""                       # optional environment variable that holds the key
    key_required: bool = True               # local OpenAI-compatible servers may not need a key

    @property
    def type_label(self) -> str:
        return PROVIDER_TYPES.get(self.provider_type, self.provider_type)

    @property
    def url(self) -> str:
        return (self.base_url or DEFAULT_BASE_URLS.get(self.provider_type, "")).rstrip("/")

    def supports(self, capability: str) -> bool:
        if capability in MEDIA_CAPABILITIES:
            return False
        if capability == "caption_correction" and "text" not in self.capabilities:
            return False
        return capability in self.capabilities

    def validate(self) -> None:
        errors = []
        if not self.display_name.strip():
            errors.append("A display name is required")
        elif len(self.display_name) > 60:
            errors.append("The display name is too long (at most 60 characters)")
        if self.provider_type not in PROVIDER_TYPES:
            errors.append(f"Unknown provider type '{self.provider_type}'")
        if not self.model.strip() or any(c.isspace() for c in self.model.strip()):
            errors.append("Enter a model name without spaces (for example gemini-2.5-flash)")
        url = urlparse(self.url)
        local = url.hostname in ("localhost", "127.0.0.1", "::1")
        if url.scheme not in ("http", "https") or not url.hostname:
            errors.append("The API base URL must start with https:// (or http:// for a server on this computer)")
        elif url.scheme == "http" and not local:
            errors.append("Use https:// for remote servers; plain http:// is only allowed for localhost")
        unknown = [c for c in self.capabilities if c not in CAPABILITIES]
        if unknown:
            errors.append(f"Unknown capabilities: {', '.join(unknown)}")
        if set(self.capabilities) & MEDIA_CAPABILITIES:
            errors.append(MEDIA_NOTE)
        if "text" not in self.capabilities:
            errors.append("Every profile needs text generation")
        if not 5 <= int(self.timeout) <= 600:
            errors.append("Timeout must be between 5 and 600 seconds")
        if not 0 <= int(self.max_retries) <= 10:
            errors.append("Retries must be between 0 and 10")
        if not 0 <= int(self.rate_limit_per_minute) <= 10_000:
            errors.append("The rate limit must be between 0 (off) and 10000 requests per minute")
        for name, value in (("Input price", self.cost_input_per_million),
                            ("Output price", self.cost_output_per_million)):
            if value is not None and not 0 <= float(value) <= 10_000:
                errors.append(f"{name} must be between 0 and 10000 per million tokens")
        if self.key_env and not ENV_NAME.match(self.key_env):
            errors.append("The environment variable name may only contain letters, digits and _")
        if self.provider_type in ("gemini", "openai") and not self.key_required:
            errors.append(f"{self.type_label} always needs an API key")
        if errors:
            raise ProfileError("; ".join(errors))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ProviderProfile":
        known = cls.__dataclass_fields__
        data = {k: v for k, v in d.items() if k in known}
        data["capabilities"] = list(data.get("capabilities") or [])
        return cls(**data)


def default_profiles(legacy: dict | None = None) -> list[ProviderProfile]:
    """The two standard profiles. ``legacy`` = earlier settings (chosen provider, model names)."""
    legacy = legacy or {}
    chosen = legacy.get("ai_correction_provider", "none")
    timeout = int(legacy.get("ai_timeout_seconds", 60) or 60)
    profiles = [
        ProviderProfile("Google Gemini", "gemini", legacy.get("gemini_model") or DEFAULT_MODELS["gemini"],
                        id="gemini", enabled=chosen == "gemini", capabilities=list(DEFAULT_CAPABILITIES["gemini"]),
                        timeout=timeout),
        ProviderProfile("OpenAI", "openai", legacy.get("openai_model") or DEFAULT_MODELS["openai"], id="openai",
                        enabled=chosen == "openai", capabilities=list(DEFAULT_CAPABILITIES["openai"]),
                        timeout=timeout),
    ]
    if chosen == "openai":
        profiles.reverse()
    return profiles


class ProviderStore:
    def __init__(self, db: Database):
        self._db = db

    def _legacy_settings(self) -> dict:
        values = {}
        for key in ("ai_correction_provider", "gemini_model", "openai_model", "ai_timeout_seconds"):
            row = self._db.query_one("SELECT value FROM settings WHERE key = ?", (key,))
            if row is not None:
                try:
                    values[key] = json.loads(row["value"])
                except ValueError:
                    pass
        return values

    def ensure_defaults(self) -> None:
        """First start with the provider manager: create the standard profiles from earlier settings."""
        if self._db.query_one("SELECT 1 FROM ai_providers LIMIT 1") is None:
            self.replace_all(default_profiles(self._legacy_settings()))

    def list(self) -> list[ProviderProfile]:
        rows = self._db.query("SELECT data FROM ai_providers ORDER BY position, rowid")
        return [ProviderProfile.from_dict(json.loads(r["data"])) for r in rows]

    def get(self, profile_id: str) -> ProviderProfile:
        row = self._db.query_one("SELECT data FROM ai_providers WHERE id = ?", (profile_id,))
        if row is None:
            raise ProfileError("This AI provider profile no longer exists.")
        return ProviderProfile.from_dict(json.loads(row["data"]))

    def save(self, profile: ProviderProfile) -> None:
        profile.validate()
        names = {p.display_name.strip().lower(): p.id for p in self.list()}
        if names.get(profile.display_name.strip().lower(), profile.id) != profile.id:
            raise ProfileError(f"Another profile is already called '{profile.display_name}'.")
        row = self._db.query_one("SELECT position FROM ai_providers WHERE id = ?", (profile.id,))
        position = row["position"] if row else self._next_position()
        self._db.execute(
            "INSERT INTO ai_providers(id, position, data, updated_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
            (profile.id, position, json.dumps(profile.to_dict()), now_iso()))

    def _next_position(self) -> int:
        row = self._db.query_one("SELECT COALESCE(MAX(position), -1) + 1 AS n FROM ai_providers")
        return int(row["n"])

    def delete(self, profile_id: str) -> None:
        self._db.execute("DELETE FROM ai_providers WHERE id = ?", (profile_id,))

    def reorder(self, ids: list[str]) -> None:
        current = [p.id for p in self.list()]
        if sorted(ids) != sorted(current):
            raise ProfileError("The new order must contain every profile exactly once.")
        with self._db.transaction() as conn:
            for position, profile_id in enumerate(ids):
                conn.execute("UPDATE ai_providers SET position = ? WHERE id = ?", (position, profile_id))

    def move(self, profile_id: str, step: int) -> None:
        ids = [p.id for p in self.list()]
        i = ids.index(profile_id)
        j = max(0, min(len(ids) - 1, i + step))
        ids.insert(j, ids.pop(i))
        self.reorder(ids)

    def set_primary(self, profile_id: str) -> None:
        ids = [p.id for p in self.list()]
        ids.remove(profile_id)
        self.reorder([profile_id] + ids)

    def replace_all(self, profiles: list[ProviderProfile]) -> None:
        for p in profiles:
            p.validate()
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM ai_providers")
            for position, p in enumerate(profiles):
                conn.execute("INSERT INTO ai_providers(id, position, data, updated_at) VALUES(?, ?, ?, ?)",
                             (p.id, position, json.dumps(p.to_dict()), now_iso()))

    def restore_defaults(self) -> None:
        """Standard Gemini and OpenAI profiles (disabled). Stored keys are not touched."""
        self.replace_all(default_profiles())

"""Caption style presets: read-only built-ins plus user presets stored in SQLite."""

from __future__ import annotations

import json

from ai_video_studio.captions.style import BUILTIN_PRESETS, CaptionStyle
from ai_video_studio.core.errors import StudioError
from ai_video_studio.core.media import now_iso
from ai_video_studio.storage.database import Database


class PresetStore:
    def __init__(self, db: Database):
        self._db = db

    def names(self) -> list[tuple[str, bool]]:
        """``[(name, is_builtin)]``: built-ins first, then user presets alphabetically."""
        rows = self._db.query("SELECT name FROM caption_presets ORDER BY name COLLATE NOCASE")
        return [(n, True) for n in BUILTIN_PRESETS] + [(r["name"], False) for r in rows]

    def get(self, name: str) -> CaptionStyle:
        if name in BUILTIN_PRESETS:
            return CaptionStyle.from_dict(BUILTIN_PRESETS[name].to_dict())
        row = self._db.query_one("SELECT style FROM caption_presets WHERE name = ?", (name,))
        if row is None:
            raise StudioError(f"Preset '{name}' does not exist")
        try:
            data = json.loads(row["style"])
        except (TypeError, ValueError) as exc:
            raise StudioError(f"The preset '{name}' is damaged and cannot be loaded. Delete it and save it again.") from exc
        if not isinstance(data, dict):
            raise StudioError(f"The preset '{name}' is damaged and cannot be loaded. Delete it and save it again.")
        return CaptionStyle.from_dict(data)

    def save(self, name: str, style: CaptionStyle) -> None:
        name = name.strip()
        if not name:
            raise StudioError("Preset name is required")
        if len(name) > 60:
            raise StudioError("Preset name is too long (max 60 characters)")
        if name in BUILTIN_PRESETS:
            raise StudioError(f"'{name}' is a built-in preset; choose another name")
        style.validate()
        self._db.execute(
            "INSERT INTO caption_presets(name, style, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET style = excluded.style, updated_at = excluded.updated_at",
            (name, json.dumps(style.to_dict()), now_iso()),
        )

    def delete(self, name: str) -> None:
        if name in BUILTIN_PRESETS:
            raise StudioError("Built-in presets cannot be deleted")
        self._db.execute("DELETE FROM caption_presets WHERE name = ?", (name,))

    def exists(self, name: str) -> bool:
        return name in BUILTIN_PRESETS or self._db.query_one(
            "SELECT 1 FROM caption_presets WHERE name = ?", (name,)) is not None

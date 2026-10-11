"""Project model and ``.avsproj`` JSON persistence."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ai_video_studio import __version__
from ai_video_studio.core.errors import ProjectError
from ai_video_studio.core.media import MediaItem, normalize_path, now_iso
from ai_video_studio.storage.database import Database

log = logging.getLogger(__name__)

PROJECT_EXTENSION = ".avsproj"
PROJECT_FORMAT = "ai-video-studio-project"
# Version history:
#   1 - Phase 1: media list with metadata
#   2 - Phase 2: per-media "captions" (CaptionTrack) and "caption_style" (optional keys)
#   3 - Phase 3: per-media "reframe" (settings + cached tracking analysis, optional key)
#   4 - Phase 3 upgrade: reframe "tracking_mode" (replaces "mode") and new tracking options;
#       analyses may contain speech, mouth movement and appearance data
#   5 - Phases 4-5: per-media "smart_cut" (audio levels, cut proposal, applied cuts) and
#       "resize" (output canvas settings); both optional, so version 1-4 files load unchanged
PROJECT_VERSION = 5


@dataclass
class Project:
    name: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(default_factory=now_iso)
    modified_at: str = field(default_factory=now_iso)
    media: list[MediaItem] = field(default_factory=list)
    path: Path | None = None
    dirty: bool = False

    # -- media ---------------------------------------------------------------
    def find_media(self, media_id: str) -> MediaItem | None:
        return next((m for m in self.media if m.id == media_id), None)

    def has_path(self, path: str) -> bool:
        target = os.path.normcase(normalize_path(path))
        return any(os.path.normcase(m.path) == target for m in self.media)

    def add_media(self, path: str) -> MediaItem | None:
        """Add a file; returns ``None`` if it is already in the project."""
        if self.has_path(path):
            return None
        item = MediaItem(path=normalize_path(path))
        self.media.append(item)
        self.dirty = True
        return item

    def remove_media(self, media_ids: set[str]) -> int:
        before = len(self.media)
        self.media = [m for m in self.media if m.id not in media_ids]
        removed = before - len(self.media)
        if removed:
            self.dirty = True
        return removed

    # -- serialisation -------------------------------------------------------
    def to_dict(self) -> dict:
        project_dir = self.path.parent if self.path else None
        return {
            "format": PROJECT_FORMAT,
            "version": PROJECT_VERSION,
            "app_version": __version__,
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "modified_at": self.modified_at,
            "media": [m.to_dict(project_dir) for m in self.media],
        }

    @classmethod
    def from_dict(cls, data: dict, path: Path | None = None) -> "Project":
        if data.get("format") != PROJECT_FORMAT:
            raise ProjectError("Not an AI Video Studio project file.")
        version = data.get("version")
        if not isinstance(version, int) or version > PROJECT_VERSION:
            raise ProjectError(
                f"Project file version {version} is newer than this application supports "
                f"({PROJECT_VERSION}). Please update AI Video Studio."
            )
        project_dir = path.parent if path else None
        try:
            media = [MediaItem.from_dict(m, project_dir) for m in data.get("media", [])]
            return cls(
                name=data["name"],
                id=data["id"],
                created_at=data.get("created_at") or now_iso(),
                modified_at=data.get("modified_at") or now_iso(),
                media=media,
                path=path,
            )
        except (KeyError, TypeError) as exc:
            raise ProjectError(f"Project file is damaged: missing or invalid field {exc}") from exc

    def save(self, path: Path | None = None) -> Path:
        target = Path(path or self.path or "")
        if not str(target) or target == Path("."):
            raise ProjectError("No file name given for the project.")
        if target.suffix.lower() != PROJECT_EXTENSION:
            target = target.with_suffix(PROJECT_EXTENSION)
        previous_path, previous_modified = self.path, self.modified_at
        self.path = target
        self.modified_at = now_iso()
        payload = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: a crash mid-save never leaves a truncated project file.
            fd, tmp = tempfile.mkstemp(prefix=".avsproj-", dir=target.parent)
        except OSError as exc:
            self.path, self.modified_at = previous_path, previous_modified
            raise ProjectError(f"Could not save project: {exc}") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            self.path, self.modified_at = previous_path, previous_modified
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise ProjectError(f"Could not save project: {exc}") from exc
        self.dirty = False
        log.info("Project saved", extra={"path": str(target), "media_count": len(self.media)})
        return target

    @classmethod
    def load(cls, path: Path | str) -> "Project":
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProjectError(f"Could not open project: {exc}") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProjectError(f"Project file is not valid JSON (line {exc.lineno}).") from exc
        if not isinstance(data, dict):
            raise ProjectError("Not an AI Video Studio project file.")
        project = cls.from_dict(data, path.resolve())
        log.info("Project loaded", extra={"path": str(path), "media_count": len(project.media)})
        return project


class RecentProjects:
    LIMIT = 15

    def __init__(self, db: Database):
        self._db = db

    def touch(self, project: Project) -> None:
        if project.path is None:
            return
        self._db.execute(
            "INSERT INTO recent_projects(path, name, last_opened) VALUES(?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET name = excluded.name, last_opened = excluded.last_opened",
            (str(project.path), project.name, now_iso()),
        )
        self._db.execute(
            "DELETE FROM recent_projects WHERE path NOT IN "
            "(SELECT path FROM recent_projects ORDER BY last_opened DESC LIMIT ?)",
            (self.LIMIT,),
        )

    def list(self) -> list[dict]:
        rows = self._db.query("SELECT path, name, last_opened FROM recent_projects ORDER BY last_opened DESC")
        return [dict(r) | {"exists": Path(r["path"]).is_file()} for r in rows]

    def remove(self, path: str) -> None:
        self._db.execute("DELETE FROM recent_projects WHERE path = ?", (path,))

"""Media items and import validation."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from ai_video_studio.captions.model import CaptionTrack
from ai_video_studio.captions.style import CaptionStyle
from ai_video_studio.core.ffmpeg_tools import VideoMetadata
from ai_video_studio.reframe.model import ReframeState
from ai_video_studio.render.effects import EditEffects
from ai_video_studio.render.layout import ResizeSettings
from ai_video_studio.smartcut.model import SmartCutState

VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".mts", ".m2ts", ".ts", ".mpg", ".mpeg"}
)


class MediaStatus(str, Enum):
    PENDING = "pending"      # metadata not read yet
    READY = "ready"          # metadata read successfully
    ERROR = "error"          # file could not be read as video
    MISSING = "missing"      # file no longer exists on disk


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class MediaItem:
    path: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    added_at: str = field(default_factory=now_iso)
    metadata: VideoMetadata | None = None
    error: str | None = None
    captions: CaptionTrack | None = None
    caption_style: CaptionStyle | None = None  # None = default style
    reframe: ReframeState | None = None        # auto reframe settings and cached tracking results
    smart_cut: SmartCutState | None = None     # audio levels, cut proposal and applied cuts
    resize: ResizeSettings | None = None       # Resize & Crop settings
    effects: EditEffects | None = None         # speed and audio effects (Edit page)

    @property
    def effective_caption_style(self) -> CaptionStyle:
        return self.caption_style or CaptionStyle()

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def status(self) -> MediaStatus:
        if not Path(self.path).is_file():
            return MediaStatus.MISSING
        if self.error:
            return MediaStatus.ERROR
        if self.metadata is None:
            return MediaStatus.PENDING
        return MediaStatus.READY

    def to_dict(self, project_dir: Path | None = None) -> dict:
        data = {
            "id": self.id,
            "path": self.path,
            "added_at": self.added_at,
            "metadata": self.metadata.to_dict() if self.metadata else None,
            "error": self.error,
        }
        if self.captions is not None:
            data["captions"] = self.captions.to_dict()
        if self.caption_style is not None:
            data["caption_style"] = self.caption_style.to_dict()
        if self.reframe is not None:
            data["reframe"] = self.reframe.to_dict()
        if self.smart_cut is not None:
            data["smart_cut"] = self.smart_cut.to_dict()
        if self.resize is not None:
            data["resize"] = self.resize.to_dict()
        if self.effects is not None:
            data["effects"] = self.effects.to_dict()
        if project_dir is not None:
            try:
                data["relative_path"] = os.path.relpath(self.path, project_dir)
            except ValueError:  # different drive on Windows
                pass
        return data

    @classmethod
    def from_dict(cls, data: dict, project_dir: Path | None = None) -> "MediaItem":
        path = data["path"]
        rel = data.get("relative_path")
        # If the project folder was moved together with its media, follow the relative path.
        if not Path(path).is_file() and rel and project_dir is not None:
            candidate = (project_dir / rel).resolve()
            if candidate.is_file():
                path = str(candidate)
        meta = data.get("metadata")
        return cls(
            path=path,
            id=data.get("id") or uuid.uuid4().hex,
            added_at=data.get("added_at") or now_iso(),
            metadata=VideoMetadata.from_dict(meta) if meta else None,
            error=data.get("error"),
            captions=CaptionTrack.from_dict(data["captions"]) if data.get("captions") else None,
            caption_style=CaptionStyle.from_dict(data["caption_style"]) if data.get("caption_style") else None,
            reframe=ReframeState.from_dict(data["reframe"]) if data.get("reframe") else None,
            smart_cut=SmartCutState.from_dict(data["smart_cut"]) if data.get("smart_cut") else None,
            resize=ResizeSettings.from_dict(data["resize"]) if data.get("resize") else None,
            effects=EditEffects.from_dict(data["effects"]) if data.get("effects") else None,
        )


def normalize_path(path: str | os.PathLike) -> str:
    return str(Path(path).expanduser().resolve())


def validate_video_path(path: str | os.PathLike) -> str | None:
    """Return an error message if ``path`` cannot be imported, otherwise ``None``."""
    p = Path(path)
    if not p.exists():
        return "File does not exist"
    if not p.is_file():
        return "Not a file"
    if p.suffix.lower() not in VIDEO_EXTENSIONS:
        return f"Unsupported file type '{p.suffix or '(none)'}'"
    if not os.access(p, os.R_OK):
        return "File is not readable"
    if p.stat().st_size == 0:
        return "File is empty"
    return None


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}.{ms // 100}"
    return f"{minutes:02d}:{secs:02d}.{ms // 100}"


def format_size(num_bytes: int | None) -> str:
    if num_bytes is None:
        return "-"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"

"""Bulk Processing: export presets, batches of videos and their job payloads (no Qt).

A batch holds a list of videos, shared settings (a :class:`RenderPreset`) and optional
per-video overrides (a complete preset for that video). Starting a batch turns each ready
video into one ``render_video`` job whose payload snapshots everything it needs: the layout
or Auto Reframe settings, captions as ASS text sized for the output, the encode settings and
the final output path. Jobs therefore survive restarts and later edits do not change them.

Built-in presets live in code and cannot be changed; user presets are stored in the user's
database (in the application data folder), so updating the application never touches them.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ai_video_studio.captions.ass import build_ass
from ai_video_studio.captions.model import CaptionSegment
from ai_video_studio.captions.style import CaptionStyle
from ai_video_studio.core.errors import StudioError
from ai_video_studio.core.ffmpeg_tools import VideoMetadata
from ai_video_studio.core.media import now_iso
from ai_video_studio.reframe.framing import ASPECT_PRESETS
from ai_video_studio.reframe.model import ReframeSettings
from ai_video_studio.render.layout import PRESETS, ResizeSettings, compute_layout
from ai_video_studio.render.naming import CONFLICT_POLICIES, render_name, validate_template
from ai_video_studio.render.output import OutputSettings
from ai_video_studio.storage.database import Database

OPERATIONS = {"resize": "Resize & Crop", "reframe": "Auto Reframe (face tracking)"}
BATCH_TRACKING_MODES = ("auto_face", "active_speaker", "speech_audio", "auto_visible", "center")
BATCH_RESIZE_MODES = ("fit", "fill", "blur", "custom")


class BatchError(StudioError):
    pass


@dataclass
class RenderPreset:
    operation: str = "resize"
    resize: ResizeSettings = field(default_factory=ResizeSettings)
    tracking_mode: str = "auto_face"     # Auto Reframe operation
    output: OutputSettings = field(default_factory=OutputSettings)
    filename_template: str = "{original_name}_{resolution}"

    def validate(self) -> None:
        if self.operation not in OPERATIONS:
            raise BatchError(f"Unknown operation '{self.operation}'")
        self.resize.validate()
        if self.operation == "resize" and self.resize.mode not in BATCH_RESIZE_MODES:
            raise BatchError("Batch Resize & Crop supports Fit, Fill, Blur background and Custom position "
                             "(a crop region or Auto Reframe is chosen per video elsewhere).")
        if self.operation == "reframe":
            if self.tracking_mode not in BATCH_TRACKING_MODES:
                raise BatchError("Manual Subject Tracking needs you to pick a person in each video; use another "
                                 "tracking mode for batches.")
            out_w, out_h = self.resize.output_size(1920, 1080)
            if self.resize.preset == "original" or not reframe_aspect(out_w, out_h):
                raise BatchError("Auto Reframe needs a 16:9, 9:16, 1:1 or 4:5 output size.")
        self.output.validate()
        validate_template(self.filename_template)

    def to_dict(self) -> dict:
        return {"operation": self.operation, "resize": self.resize.to_dict(), "tracking_mode": self.tracking_mode,
                "output": self.output.to_dict(), "filename_template": self.filename_template}

    @classmethod
    def from_dict(cls, d: dict | None) -> "RenderPreset":
        d = d or {}
        return cls(operation=d.get("operation", "resize"), resize=ResizeSettings.from_dict(d.get("resize")),
                   tracking_mode=d.get("tracking_mode", "auto_face"), output=OutputSettings.from_dict(d.get("output")),
                   filename_template=d.get("filename_template", "{original_name}_{resolution}"))

    def copy(self) -> "RenderPreset":
        return RenderPreset.from_dict(json.loads(json.dumps(self.to_dict())))


def reframe_aspect(out_w: int, out_h: int) -> str | None:
    return next((name for name, ratio in ASPECT_PRESETS.items()
                 if ratio and abs(ratio[0] / ratio[1] - out_w / out_h) < 0.01), None)


BUILTIN_RENDER_PRESETS: dict[str, RenderPreset] = {
    "Vertical Reels 1080x1920": RenderPreset(resize=ResizeSettings(preset="1080x1920", mode="blur"),
                                             filename_template="{original_name}_vertical"),
    "YouTube 1920x1080": RenderPreset(resize=ResizeSettings(preset="1920x1080", mode="fit"),
                                      filename_template="{original_name}_youtube"),
    "Square 1080x1080": RenderPreset(resize=ResizeSettings(preset="1080x1080", mode="blur"),
                                     filename_template="{original_name}_square"),
    "Vertical Reels with captions": RenderPreset(
        resize=ResizeSettings(preset="1080x1920", mode="blur", burn_captions=True),
        filename_template="{original_name}_captioned"),
    "Vertical Auto Reframe 1080x1920": RenderPreset(operation="reframe", resize=ResizeSettings(preset="1080x1920"),
                                                    filename_template="{original_name}_reframed"),
}


class RenderPresetStore:
    def __init__(self, db: Database):
        self._db = db

    def names(self) -> list[tuple[str, bool]]:
        rows = self._db.query("SELECT name FROM render_presets ORDER BY name COLLATE NOCASE")
        return [(n, True) for n in BUILTIN_RENDER_PRESETS] + [(r["name"], False) for r in rows]

    def get(self, name: str) -> RenderPreset:
        if name in BUILTIN_RENDER_PRESETS:
            return BUILTIN_RENDER_PRESETS[name].copy()
        row = self._db.query_one("SELECT data FROM render_presets WHERE name = ?", (name,))
        if row is None:
            raise BatchError(f"Preset '{name}' does not exist")
        return RenderPreset.from_dict(json.loads(row["data"]))

    def exists(self, name: str) -> bool:
        return name in BUILTIN_RENDER_PRESETS or self._db.query_one(
            "SELECT 1 FROM render_presets WHERE name = ?", (name,)) is not None

    @staticmethod
    def _check_name(name: str) -> str:
        name = name.strip()
        if not name:
            raise BatchError("A preset name is required.")
        if len(name) > 60:
            raise BatchError("The preset name is too long (at most 60 characters).")
        if name in BUILTIN_RENDER_PRESETS:
            raise BatchError(f"'{name}' is a built-in preset; choose another name.")
        return name

    def save(self, name: str, preset: RenderPreset) -> str:
        name = self._check_name(name)
        preset.validate()
        self._db.execute(
            "INSERT INTO render_presets(name, data, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
            (name, json.dumps(preset.to_dict()), now_iso()))
        return name

    def rename(self, old: str, new: str) -> str:
        if old in BUILTIN_RENDER_PRESETS:
            raise BatchError("Built-in presets cannot be renamed.")
        new = self._check_name(new)
        if new != old and self.exists(new):
            raise BatchError(f"A preset named '{new}' already exists.")
        with self._db.transaction() as conn:
            if conn.execute("SELECT 1 FROM render_presets WHERE name = ?", (old,)).fetchone() is None:
                raise BatchError(f"Preset '{old}' does not exist")
            conn.execute("UPDATE render_presets SET name = ?, updated_at = ? WHERE name = ?", (new, now_iso(), old))
        return new

    def delete(self, name: str) -> None:
        if name in BUILTIN_RENDER_PRESETS:
            raise BatchError("Built-in presets cannot be deleted.")
        self._db.execute("DELETE FROM render_presets WHERE name = ?", (name,))


@dataclass
class BatchItem:
    path: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    metadata: VideoMetadata | None = None
    error: str | None = None
    override: RenderPreset | None = None
    job_id: str | None = None
    output: str | None = None
    note: str = ""
    skipped: bool = False

    @property
    def name(self) -> str:
        return Path(self.path).name

    def to_dict(self) -> dict:
        return {"path": self.path, "id": self.id, "metadata": self.metadata.to_dict() if self.metadata else None,
                "error": self.error, "override": self.override.to_dict() if self.override else None,
                "job_id": self.job_id, "output": self.output, "note": self.note, "skipped": self.skipped}

    @classmethod
    def from_dict(cls, d: dict) -> "BatchItem":
        return cls(path=d["path"], id=d.get("id") or uuid.uuid4().hex[:12],
                   metadata=VideoMetadata.from_dict(d["metadata"]) if d.get("metadata") else None,
                   error=d.get("error"), override=RenderPreset.from_dict(d["override"]) if d.get("override") else None,
                   job_id=d.get("job_id"), output=d.get("output"), note=d.get("note", ""),
                   skipped=bool(d.get("skipped")))


@dataclass
class Batch:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "Batch"
    items: list[BatchItem] = field(default_factory=list)
    shared: RenderPreset = field(default_factory=RenderPreset)
    preset_name: str = ""
    output_dir: str = ""
    conflict: str = "rename"
    created_at: str = field(default_factory=now_iso)

    def find(self, item_id: str) -> BatchItem | None:
        return next((i for i in self.items if i.id == item_id), None)

    def effective(self, item: BatchItem) -> RenderPreset:
        return item.override if item.override is not None else self.shared

    def add(self, paths: list[str]) -> tuple[list[BatchItem], list[str]]:
        """Add files; returns ``(added, duplicates)``. A file is only listed once."""
        import os

        known = {os.path.normcase(str(Path(i.path).resolve())) for i in self.items}
        added, duplicates = [], []
        for path in paths:
            key = os.path.normcase(str(Path(path).resolve()))
            if key in known:
                duplicates.append(path)
                continue
            known.add(key)
            item = BatchItem(str(Path(path).resolve()))
            self.items.append(item)
            added.append(item)
        return added, duplicates

    def remove(self, ids: set[str]) -> int:
        before = len(self.items)
        self.items = [i for i in self.items if i.id not in ids]
        return before - len(self.items)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "items": [i.to_dict() for i in self.items],
                "shared": self.shared.to_dict(), "preset_name": self.preset_name, "output_dir": self.output_dir,
                "conflict": self.conflict, "created_at": self.created_at}

    @classmethod
    def from_dict(cls, d: dict) -> "Batch":
        return cls(id=d["id"], name=d.get("name", "Batch"), items=[BatchItem.from_dict(i) for i in d.get("items", [])],
                   shared=RenderPreset.from_dict(d.get("shared")), preset_name=d.get("preset_name", ""),
                   output_dir=d.get("output_dir", ""), conflict=d.get("conflict", "rename"),
                   created_at=d.get("created_at") or now_iso())


class BatchStore:
    """Saves the batch being edited so it survives restarts (and crashes)."""

    def __init__(self, db: Database):
        self._db = db

    def save(self, batch: Batch) -> None:
        self._db.execute(
            "INSERT INTO batches(id, name, data, created_at, updated_at) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, data = excluded.data, updated_at = excluded.updated_at",
            (batch.id, batch.name, json.dumps(batch.to_dict()), batch.created_at, now_iso()))

    def latest(self) -> Batch | None:
        row = self._db.query_one("SELECT data FROM batches ORDER BY updated_at DESC, rowid DESC LIMIT 1")
        return Batch.from_dict(json.loads(row["data"])) if row else None

    def delete(self, batch_id: str) -> None:
        self._db.execute("DELETE FROM batches WHERE id = ?", (batch_id,))


def output_size(preset: RenderPreset, meta: VideoMetadata) -> tuple[int, int]:
    return preset.resize.output_size(meta.display_width, meta.display_height)


def output_name(preset: RenderPreset, item: BatchItem, index: int, preset_name: str) -> str:
    w, h = output_size(preset, item.metadata)
    fields = {"original_name": Path(item.path).stem, "preset": preset_name or "custom", "resolution": f"{w}x{h}",
              "width": w, "height": h, "codec": preset.output.codec, "index": f"{index:03d}"}
    return render_name(preset.filename_template, fields) + ".mp4"


def build_payload(item: BatchItem, preset: RenderPreset, output: Path, *, overwrite: bool, batch_id: str,
                  captions: list[CaptionSegment] | None, caption_style: CaptionStyle | None,
                  tracking_defaults: ReframeSettings, models_dir: str) -> tuple[dict, list[str]]:
    """The ``render_video`` payload for one batch video and notes for the user."""
    preset.validate()
    meta = item.metadata
    if meta is None:
        raise BatchError(f"{item.name}: video information has not been read.")
    notes: list[str] = []
    w, h = output_size(preset, meta)
    ass = None
    style = caption_style or CaptionStyle()
    if preset.resize.burn_captions:
        if captions:
            ass = build_ass(captions, style, w, h, title=item.name)
        else:
            notes.append("No captions found (none in the project and no .srt file with the same name); "
                         "exported without captions.")
    payload = {
        "path": item.path, "media_id": None, "output": str(output), "overwrite": overwrite,
        "duration": meta.duration, "fps": meta.fps, "has_audio": meta.has_audio, "audio_codec": meta.audio_codec,
        "keep_ranges": None, "ass": ass, "output_settings": preset.output.to_dict(),
        "batch_id": batch_id, "batch_item": item.id,
    }
    if preset.operation == "reframe":
        settings = ReframeSettings.from_dict(tracking_defaults.to_dict())
        settings.tracking_mode = preset.tracking_mode
        settings.preset = reframe_aspect(w, h)
        settings.output_width, settings.output_height = w, h
        settings.target, settings.switches = None, []
        settings.validate()
        features = settings.analysis_features()
        if "speech" in features and not meta.has_audio:
            raise BatchError(f"{item.name}: {settings.mode_info.label} needs an audio track.")
        payload["reframe_auto"] = {
            "settings": settings.to_dict(),
            "caption_style": style.to_dict() if ass else None,
            "analysis": {"width": meta.display_width, "height": meta.display_height, "duration": meta.duration,
                         "sample_fps": settings.analysis_rate(), "detect_persons": "persons" in features,
                         "features": features, "face_threshold": settings.confidence_threshold,
                         "models_dir": models_dir},
        }
    else:
        layout = compute_layout(meta.display_width, meta.display_height, preset.resize)
        payload["stage"] = {"type": "layout", "layout": layout.to_dict()}
        if layout.upscaled:
            notes.append(f"Enlarged {layout.scale:.2f}×.")
    return payload, notes


def item_progress(states: list[tuple[str, float]]) -> float:
    """Overall progress of a batch from ``(status, job progress)`` per started or skipped video."""
    if not states:
        return 0.0
    done = sum(1.0 if status in ("completed", "failed", "cancelled", "skipped") else
               (progress if status in ("analyzing", "rendering") else 0.0) for status, progress in states)
    return done / len(states)


PRESET_SIZE_LABELS = {k: v[0] for k, v in PRESETS.items()}
CONFLICT_LABELS = CONFLICT_POLICIES

"""Bulk Processing: export presets, batches of videos and their job payloads (no Qt).

A batch holds a list of videos, shared settings (a :class:`RenderPreset`) and optional
per-video overrides (a complete preset for that video). A preset combines every operation
with its own switch: framing (Resize & Crop, Auto Reframe or the original frame), Smart Cut
silence removal, burned-in captions with an optional caption style, speed and audio effects,
the encode settings and the file name. Starting a batch turns each ready video into one
``render_video`` job whose payload snapshots everything it needs (captions as segments plus
style, timed inside the job after the cuts are known), so all operations are encoded once,
jobs survive restarts, and later edits do not change them.

Built-in presets live in code and cannot be changed; user presets are stored in the user's
database (in the application data folder), so updating the application never touches them.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ai_video_studio.captions.model import CaptionSegment
from ai_video_studio.captions.style import CaptionStyle
from ai_video_studio.core.errors import StudioError
from ai_video_studio.core.ffmpeg_tools import VideoMetadata
from ai_video_studio.core.media import now_iso
from ai_video_studio.reframe.framing import ASPECT_PRESETS
from ai_video_studio.reframe.model import ReframeSettings
from ai_video_studio.render.effects import EditEffects
from ai_video_studio.render.layout import PRESETS, ResizeSettings, compute_layout
from ai_video_studio.render.naming import CONFLICT_POLICIES, render_name, validate_template
from ai_video_studio.render.output import OutputSettings
from ai_video_studio.smartcut.model import SmartCutSettings
from ai_video_studio.storage.database import Database

OPERATIONS = {"resize": "Resize & Crop", "reframe": "Auto Reframe (face tracking)",
              "none": "Keep the original frame"}
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
    smart_cut: bool = False              # remove silence (automatic detection, or the project's reviewed cuts)
    smart_cut_settings: SmartCutSettings = field(default_factory=SmartCutSettings)
    caption_style: CaptionStyle | None = None  # None = each video's own caption style
    auto_captions: bool = False          # burn-in: generate each video's captions with Whisper in its job
    effects: EditEffects = field(default_factory=EditEffects)

    def validate(self) -> None:
        if self.operation not in OPERATIONS:
            raise BatchError(f"Unknown operation '{self.operation}'")
        self.smart_cut_settings.validate()
        self.effects.validate()
        if self.caption_style is not None:
            self.caption_style.validate()
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
                "output": self.output.to_dict(), "filename_template": self.filename_template,
                "smart_cut": self.smart_cut, "smart_cut_settings": self.smart_cut_settings.to_dict(),
                "caption_style": self.caption_style.to_dict() if self.caption_style else None,
                "auto_captions": self.auto_captions, "effects": self.effects.to_dict()}

    @classmethod
    def from_dict(cls, d: dict | None) -> "RenderPreset":
        """Read a stored preset; presets saved before v0.8 have no Smart Cut, caption style or effects."""
        d = d or {}
        try:
            cut_settings = SmartCutSettings.from_dict(d.get("smart_cut_settings"))
            cut_settings.validate()
        except (StudioError, TypeError, ValueError):
            cut_settings = SmartCutSettings()
        return cls(operation=d.get("operation", "resize"), resize=ResizeSettings.from_dict(d.get("resize")),
                   tracking_mode=d.get("tracking_mode", "auto_face"), output=OutputSettings.from_dict(d.get("output")),
                   filename_template=d.get("filename_template", "{original_name}_{resolution}"),
                   smart_cut=bool(d.get("smart_cut", False)), smart_cut_settings=cut_settings,
                   caption_style=CaptionStyle.from_dict(d["caption_style"]) if d.get("caption_style") else None,
                   auto_captions=bool(d.get("auto_captions", False)), effects=EditEffects.from_dict(d.get("effects")))

    def copy(self) -> "RenderPreset":
        return RenderPreset.from_dict(json.loads(json.dumps(self.to_dict())))


def preset_from_edit(resize: ResizeSettings | None, *, use_cuts: bool, smart_cut_settings: SmartCutSettings,
                     burn_captions: bool, caption_style: CaptionStyle | None, effects: EditEffects,
                     tracking_mode: str, output: OutputSettings) -> RenderPreset:
    """Bulk settings equivalent to one video's Edit page settings (for Bulk Processing).

    Framing: Resize & Crop, Auto Reframe or the original frame; Smart Cut detects silence in every
    video with the same settings; the caption style is used for every video's captions.
    """
    on = resize is not None and resize.enabled
    r = ResizeSettings.from_dict(resize.to_dict()) if resize else ResizeSettings()
    r.burn_captions, r.use_cuts = burn_captions, False
    operation = ("reframe" if r.mode == "reframe" else "resize") if on else "none"
    if operation == "reframe":
        r.mode = "fill"  # unused by Auto Reframe; keeps the preset valid
    preset = RenderPreset(operation=operation, resize=r, tracking_mode=tracking_mode, output=output,
                          filename_template="{original_name}_edited", smart_cut=use_cuts,
                          smart_cut_settings=SmartCutSettings.from_dict(smart_cut_settings.to_dict()),
                          caption_style=CaptionStyle.from_dict(caption_style.to_dict()) if burn_captions and
                          caption_style else None, effects=effects.copy())
    preset.validate()
    return preset


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
        try:
            data = json.loads(row["data"])
            if not isinstance(data, dict):
                raise ValueError("not an object")
            return RenderPreset.from_dict(data)
        except (TypeError, ValueError) as exc:
            raise BatchError(f"The preset '{name}' is damaged and cannot be loaded. Delete it and save it "
                             "again.") from exc

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
    selected: bool = True     # ticked in the list: Start Batch and Apply to Selected use ticked videos

    @property
    def name(self) -> str:
        return Path(self.path).name

    def to_dict(self) -> dict:
        return {"path": self.path, "id": self.id, "metadata": self.metadata.to_dict() if self.metadata else None,
                "error": self.error, "override": self.override.to_dict() if self.override else None,
                "job_id": self.job_id, "output": self.output, "note": self.note, "skipped": self.skipped,
                "selected": self.selected}

    @classmethod
    def from_dict(cls, d: dict) -> "BatchItem":
        return cls(path=d["path"], id=d.get("id") or uuid.uuid4().hex[:12],
                   metadata=VideoMetadata.from_dict(d["metadata"]) if d.get("metadata") else None,
                   error=d.get("error"), override=RenderPreset.from_dict(d["override"]) if d.get("override") else None,
                   job_id=d.get("job_id"), output=d.get("output"), note=d.get("note", ""),
                   skipped=bool(d.get("skipped")), selected=bool(d.get("selected", True)))


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
    if preset.operation == "none":
        return meta.display_width, meta.display_height
    return preset.resize.output_size(meta.display_width, meta.display_height)


def output_name(preset: RenderPreset, item: BatchItem, index: int, preset_name: str) -> str:
    w, h = output_size(preset, item.metadata)
    fields = {"original_name": Path(item.path).stem, "preset": preset_name or "custom", "resolution": f"{w}x{h}",
              "width": w, "height": h, "codec": preset.output.codec, "index": f"{index:03d}"}
    return render_name(preset.filename_template, fields) + ".mp4"


def build_payload(item: BatchItem, preset: RenderPreset, output: Path, *, overwrite: bool, batch_id: str,
                  captions: list[CaptionSegment] | None, caption_style: CaptionStyle | None,
                  tracking_defaults: ReframeSettings, models_dir: str,
                  reviewed_cuts: list[tuple[float, float]] | None = None,
                  transcription: dict | None = None) -> tuple[dict, list[str]]:
    """The ``render_video`` payload for one batch video and notes for the user.

    ``reviewed_cuts``: the video's Smart Cut cuts applied in the open project; with Smart Cut on
    they are used instead of automatic detection.
    ``transcription``: Whisper settings (model, language, device, model folder, caption length) for
    automatic captions; the job transcribes this video itself (``captions_auto``), so captions never
    come from another video and need no project or .srt file.
    """
    preset.validate()
    meta = item.metadata
    if meta is None:
        raise BatchError(f"{item.name}: video information has not been read.")
    notes: list[str] = []
    w, h = output_size(preset, meta)
    style = preset.caption_style or caption_style or CaptionStyle()
    caption_data = None
    auto = preset.resize.burn_captions and preset.auto_captions
    if auto:
        if not transcription:
            raise BatchError("Automatic captions need the transcription settings.")
        caption_data = {"segments": [], "style": style.to_dict(), "width": w, "height": h, "title": item.name}
    elif preset.resize.burn_captions:
        if captions:
            caption_data = {"segments": [s.to_dict() for s in captions], "style": style.to_dict(), "width": w,
                            "height": h, "title": item.name}
        else:
            notes.append("No captions found (none in the project and no .srt file with the same name); "
                         "exported without captions.")
    payload = {
        "path": item.path, "media_id": None, "output": str(output), "overwrite": overwrite,
        "duration": meta.duration, "fps": meta.fps, "has_audio": meta.has_audio, "audio_codec": meta.audio_codec,
        "keep_ranges": None, "captions": caption_data, "output_settings": preset.output.to_dict(),
        "batch_id": batch_id, "batch_item": item.id,
        "effects": preset.effects.to_dict() if preset.effects.active else None,
        "captions_auto": dict(transcription) if auto else None,
    }
    if preset.smart_cut:
        if not meta.has_audio:
            notes.append("Smart Cut skipped: the video has no audio track.")
        elif reviewed_cuts:
            payload["keep_ranges"] = [list(r) for r in reviewed_cuts]
            notes.append("Smart Cut: the cuts reviewed in the project are used.")
        else:
            payload["smartcut_auto"] = {"settings": preset.smart_cut_settings.to_dict()}
    if preset.effects.audio_active and not meta.has_audio:
        notes.append("Audio effects skipped: the video has no audio track.")
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
            "caption_style": style.to_dict() if caption_data else None,
            "analysis": {"width": meta.display_width, "height": meta.display_height, "duration": meta.duration,
                         "sample_fps": settings.analysis_rate(), "detect_persons": "persons" in features,
                         "features": features, "face_threshold": settings.confidence_threshold,
                         "models_dir": models_dir},
        }
    elif preset.operation == "resize":
        layout = compute_layout(meta.display_width, meta.display_height, preset.resize)
        payload["stage"] = {"type": "layout", "layout": layout.to_dict()}
        if layout.upscaled:
            notes.append(f"Enlarged {layout.scale:.2f}×.")
    return payload, notes


def frame_preview_payload(item: BatchItem, preset: RenderPreset, captions: list[CaptionSegment] | None,
                          caption_style: CaptionStyle | None) -> tuple[dict, list[str]]:
    """A ``render_frame_preview`` payload showing one frame of a batch video with these settings.

    It shows the framing and the caption style. Auto Reframe follows faces only during the export,
    so its preview shows the centre crop; speed and audio cannot be seen in a still frame.
    """
    from ai_video_studio.captions.ass import build_ass

    preset.validate()
    meta = item.metadata
    if meta is None:
        raise BatchError(f"{item.name}: video information has not been read.")
    w, h = output_size(preset, meta)
    notes = []
    stage = None
    if preset.operation == "resize":
        stage = {"type": "layout", "layout": compute_layout(meta.display_width, meta.display_height,
                                                            preset.resize).to_dict()}
    elif preset.operation == "reframe":
        fill = ResizeSettings.from_dict(preset.resize.to_dict())
        fill.mode = "fill"
        stage = {"type": "layout", "layout": compute_layout(meta.display_width, meta.display_height, fill).to_dict()}
        notes.append("Auto Reframe follows faces during the export; this preview shows the centre crop.")
    at = meta.duration / 2 if meta.duration else 0.0
    ass = None
    if preset.resize.burn_captions and captions:
        style = preset.caption_style or caption_style or CaptionStyle()
        shown = [c for c in captions if c.start <= at <= c.end] or captions[:1]
        at = at if any(c.start <= at <= c.end for c in captions) else (shown[0].start + shown[0].end) / 2
        ass = build_ass(shown, style, w, h, title=item.name)
    if preset.effects.active:
        notes.append("Speed and audio effects are applied in the export (not visible in a still frame).")
    return {"path": item.path, "media_id": None, "at": at, "stage": stage, "ass": ass}, notes


def item_progress(states: list[tuple[str, float]]) -> float:
    """Overall progress of a batch from ``(status, job progress)`` per started or skipped video."""
    if not states:
        return 0.0
    done = sum(1.0 if status in ("completed", "failed", "cancelled", "skipped") else
               (progress if status in ("analyzing", "rendering") else 0.0) for status, progress in states)
    return done / len(states)


PRESET_SIZE_LABELS = {k: v[0] for k, v in PRESETS.items()}
CONFLICT_LABELS = CONFLICT_POLICIES

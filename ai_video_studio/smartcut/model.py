"""Silence detection, the reviewable segment list and the applied cut list.

A Smart Cut proposal divides the whole clip into consecutive segments: ``speech`` (sound
above the threshold, kept) and ``silence`` (proposed for removal). The user reviews it, can
keep any silence, remove any speech, and move boundaries. Nothing is cut until the user
applies the proposal; the *applied* keep ranges are what exports use. Source videos and
caption tracks are never changed: captions are remapped to the edited timeline at export.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field

from ai_video_studio.core.errors import StudioError
from ai_video_studio.smartcut.levels import AudioLevels

MIN_SEGMENT = 0.05  # seconds


class SmartCutError(StudioError):
    pass


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


@dataclass
class SmartCutSettings:
    threshold_db: float = -35.0   # a window is silent when its peak level is below this
    min_silence: float = 0.6      # shorter quiet gaps are kept (natural pauses)
    pad_before: float = 0.15      # sound kept before speech starts
    pad_after: float = 0.25       # sound kept after speech ends
    min_keep: float = 0.3         # sound shorter than this between two silences is cut with them
    merge_gap: float = 0.0        # 0 = off; silences shorter than this after padding are kept

    def validate(self) -> None:
        errors = []
        if not -90 <= self.threshold_db <= -5:
            errors.append("Silence threshold must be between -90 and -5 dB")
        if not 0.1 <= self.min_silence <= 30:
            errors.append("Minimum silence must be between 0.1 and 30 s")
        for name, value in (("Padding before speech", self.pad_before), ("Padding after speech", self.pad_after)):
            if not 0 <= value <= 5:
                errors.append(f"{name} must be between 0 and 5 s")
        if not 0 <= self.min_keep <= 10:
            errors.append("Minimum kept segment must be between 0 and 10 s")
        if not 0 <= self.merge_gap <= 30:
            errors.append("Merge gap must be between 0 and 30 s")
        if errors:
            raise SmartCutError("; ".join(errors))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "SmartCutSettings":
        known = cls.__dataclass_fields__
        return cls(**{k: float(v) for k, v in (d or {}).items() if k in known})


@dataclass
class CutSegment:
    start: float
    end: float
    kind: str            # speech | silence
    remove: bool
    id: str = field(default_factory=_new_id)
    note: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CutSegment":
        return cls(start=float(d["start"]), end=float(d["end"]), kind=str(d.get("kind", "speech")),
                   remove=bool(d.get("remove", False)), id=str(d.get("id") or _new_id()), note=str(d.get("note", "")))


# -- detection --------------------------------------------------------------------

def _quiet_runs(levels: AudioLevels, threshold: float, duration: float) -> list[tuple[float, float]]:
    runs, start = [], None
    for i, value in enumerate(levels.peak):
        if value < threshold:
            if start is None:
                start = i
        elif start is not None:
            runs.append((start * levels.window, i * levels.window))
            start = None
    if start is not None:
        runs.append((start * levels.window, len(levels.peak) * levels.window))
    covered = len(levels.peak) * levels.window
    if covered < duration - levels.window:  # no audio data at the end (audio shorter than video)
        if runs and abs(runs[-1][1] - covered) < 1e-9:
            runs[-1] = (runs[-1][0], duration)
        else:
            runs.append((covered, duration))
    return [(max(0.0, a), min(duration, b)) for a, b in runs if min(duration, b) > a]


def detect_silences(levels: AudioLevels, settings: SmartCutSettings, duration: float | None = None
                    ) -> list[tuple[float, float]]:
    """Time ranges proposed for removal (already shrunk by the speech padding)."""
    settings.validate()
    duration = float(duration or levels.duration)
    silences = []
    for a, b in _quiet_runs(levels, settings.threshold_db, duration):
        if b - a < settings.min_silence:
            continue
        # Keep a little sound around speech; no padding is needed at the clip edges.
        a2 = a + (settings.pad_after if a > 0 else 0.0)
        b2 = b - (settings.pad_before if b < duration else 0.0)
        if b2 - a2 >= MIN_SEGMENT:
            silences.append((round(a2, 3), round(b2, 3)))
    if settings.merge_gap > 0:
        silences = [(a, b) for a, b in silences if b - a >= settings.merge_gap]
    return silences


def build_segments(silences: list[tuple[float, float]], duration: float, min_keep: float = 0.0) -> list[CutSegment]:
    """Turn silences into a full partition of [0, duration]; short sounds between silences join them."""
    merged: list[list] = []  # [start, end, note]
    for a, b in silences:
        if merged and a - merged[-1][1] < min_keep and a > merged[-1][1] - 1e-9:
            gap = a - merged[-1][1]
            note = f"contains a short sound ({gap:.2f} s)" if gap > 0.001 else ""
            merged[-1][1] = b
            merged[-1][2] = "; ".join(x for x in (merged[-1][2], note) if x)
        else:
            merged.append([a, b, ""])
    segments, cursor = [], 0.0
    for a, b, note in merged:
        if a - cursor >= MIN_SEGMENT:
            segments.append(CutSegment(round(cursor, 3), round(a, 3), "speech", False))
        elif segments:  # tiny sliver of sound: absorb it into the previous silence
            segments[-1].end = round(b, 3)
            cursor = b
            continue
        else:
            a = 0.0
        segments.append(CutSegment(round(a, 3), round(b, 3), "silence", True, note=note))
        cursor = b
    if duration - cursor >= MIN_SEGMENT or not segments:
        segments.append(CutSegment(round(cursor, 3), round(duration, 3), "speech", False))
    elif segments:
        segments[-1].end = round(duration, 3)
    return segments


def detection_notes(levels: AudioLevels, segments: list[CutSegment], settings: SmartCutSettings) -> list[str]:
    notes = []
    duration = levels.duration or (segments[-1].end if segments else 0)
    silent = sum(s.duration for s in segments if s.kind == "silence")
    if not any(v > -100 for v in levels.peak):
        notes.append("The audio track is silent.")
    elif silent == 0:
        notes.append(f"No silence below {settings.threshold_db:g} dB lasting {settings.min_silence:g} s was found. "
                     "If there is background music or noise, try a higher threshold (e.g. -30 dB).")
    elif duration and silent / duration > 0.8:
        notes.append(f"{silent / duration:.0%} of the clip is below the threshold. For quiet dialogue, "
                     "try a lower threshold (e.g. -45 dB) so speech is not proposed for removal.")
    return notes


# -- editing --------------------------------------------------------------------------

def keep_ranges(segments: list[CutSegment]) -> list[tuple[float, float]]:
    """Kept parts of the clip, with neighbouring kept segments joined."""
    ranges: list[tuple[float, float]] = []
    for seg in segments:
        if seg.remove:
            continue
        if ranges and abs(ranges[-1][1] - seg.start) < 1e-6:
            ranges[-1] = (ranges[-1][0], seg.end)
        else:
            ranges.append((seg.start, seg.end))
    return ranges


def set_remove(segments: list[CutSegment], ids: set[str], remove: bool) -> int:
    changed = 0
    for seg in segments:
        if seg.id in ids and seg.remove != remove:
            seg.remove = remove
            changed += 1
    return changed


def set_bounds(segments: list[CutSegment], seg_id: str, start: float, end: float) -> None:
    """Move a segment's boundaries; the neighbours shrink or grow so the list stays a partition."""
    index = next((i for i, s in enumerate(segments) if s.id == seg_id), None)
    if index is None:
        raise SmartCutError("Segment not found")
    seg = segments[index]
    start, end = round(float(start), 3), round(float(end), 3)
    lower = segments[index - 1].start + MIN_SEGMENT if index > 0 else 0.0
    upper = segments[index + 1].end - MIN_SEGMENT if index + 1 < len(segments) else seg.end
    if index == 0 and start != 0.0:
        raise SmartCutError("The first segment always starts at 0.")
    if index == len(segments) - 1 and end != seg.end:
        raise SmartCutError("The last segment always ends at the end of the clip.")
    if end - start < MIN_SEGMENT:
        raise SmartCutError("A segment must be at least 50 ms long.")
    if start < lower - 1e-9 or end > upper + 1e-9:
        raise SmartCutError("The new times would overlap a neighbouring segment completely.")
    seg.start, seg.end = start, end
    if index > 0:
        segments[index - 1].end = start
    if index + 1 < len(segments):
        segments[index + 1].start = end


class EditHistory:
    """Undo/redo snapshots of a segment list."""

    LIMIT = 200

    def __init__(self) -> None:
        self._undo: list[list[dict]] = []
        self._redo: list[list[dict]] = []

    def record(self, segments: list[CutSegment]) -> None:
        """Call before changing ``segments``."""
        self.push([s.to_dict() for s in segments])

    def push(self, snapshot: list[dict]) -> None:
        self._undo.append(snapshot)
        del self._undo[:-self.LIMIT]
        self._redo.clear()

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self, current: list[CutSegment]) -> list[CutSegment]:
        if not self._undo:
            return current
        self._redo.append([s.to_dict() for s in current])
        return [CutSegment.from_dict(d) for d in self._undo.pop()]

    def redo(self, current: list[CutSegment]) -> list[CutSegment]:
        if not self._redo:
            return current
        self._undo.append([s.to_dict() for s in current])
        return [CutSegment.from_dict(d) for d in self._redo.pop()]


# -- per-video state -------------------------------------------------------------------

@dataclass
class SmartCutState:
    settings: SmartCutSettings = field(default_factory=SmartCutSettings)
    levels: AudioLevels | None = None
    segments: list[CutSegment] = field(default_factory=list)
    applied: list[tuple[float, float]] | None = None  # keep ranges used by exports

    @property
    def duration(self) -> float:
        if self.segments:
            return self.segments[-1].end
        return self.levels.duration if self.levels else 0.0

    def detect(self) -> None:
        """Rebuild the proposal from the stored levels and current settings."""
        if self.levels is None:
            raise SmartCutError("Analyse the audio first.")
        silences = detect_silences(self.levels, self.settings)
        self.segments = build_segments(silences, self.levels.duration, self.settings.min_keep)

    def proposal_ranges(self) -> list[tuple[float, float]]:
        return keep_ranges(self.segments)

    @property
    def has_pending_changes(self) -> bool:
        """The reviewed proposal differs from what is applied."""
        proposal = [(round(a, 3), round(b, 3)) for a, b in self.proposal_ranges()]
        applied = [(round(a, 3), round(b, 3)) for a, b in (self.applied or [(0.0, round(self.duration, 3))])]
        return bool(self.segments) and proposal != applied

    def apply(self) -> list[tuple[float, float]]:
        ranges = self.proposal_ranges()
        if not ranges:
            raise SmartCutError("Every segment is marked for removal; keep at least one.")
        self.applied = [(round(a, 3), round(b, 3)) for a, b in ranges]
        return self.applied

    def cut_ranges(self) -> list[tuple[float, float]] | None:
        """Applied keep ranges when they actually cut something, else None."""
        if not self.applied:
            return None
        if len(self.applied) == 1 and self.applied[0][0] <= 0.001 and self.applied[0][1] >= self.duration - 0.001:
            return None
        return list(self.applied)

    def to_dict(self) -> dict:
        return {
            "settings": self.settings.to_dict(),
            "levels": self.levels.to_dict() if self.levels else None,
            "segments": [s.to_dict() for s in self.segments],
            "applied": [list(r) for r in self.applied] if self.applied is not None else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SmartCutState":
        return cls(
            settings=SmartCutSettings.from_dict(d.get("settings")),
            levels=AudioLevels.from_dict(d["levels"]) if d.get("levels") else None,
            segments=[CutSegment.from_dict(s) for s in d.get("segments", [])],
            applied=[(float(a), float(b)) for a, b in d["applied"]] if d.get("applied") is not None else None,
        )

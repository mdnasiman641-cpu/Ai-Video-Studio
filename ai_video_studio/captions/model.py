"""Caption segments, validation and editing operations (no Qt).

Times are seconds (float), rounded to milliseconds. Segments in a track are kept
sorted by start time. Overlaps are allowed (they render stacked) but reported by
:func:`find_overlaps` so the UI can warn about them.
"""

from __future__ import annotations

import bisect
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ai_video_studio.core.errors import StudioError

MIN_SEGMENT_DURATION = 0.05  # seconds
DEFAULT_NEW_SEGMENT_DURATION = 2.0


class CaptionError(StudioError):
    """An invalid caption edit."""


def _ms(value: float) -> float:
    return round(float(value), 3)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Word:
    start: float
    end: float
    text: str  # Whisper word text, usually with a leading space

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "text": self.text}

    @classmethod
    def from_dict(cls, d: dict) -> "Word":
        return cls(_ms(d["start"]), _ms(d["end"]), str(d["text"]))


@dataclass
class CaptionSegment:
    start: float
    end: float
    text: str
    id: str = field(default_factory=_new_id)
    words: list[Word] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.start, self.end = _ms(self.start), _ms(self.end)

    @property
    def duration(self) -> float:
        return _ms(self.end - self.start)

    def to_dict(self) -> dict:
        data = {"id": self.id, "start": self.start, "end": self.end, "text": self.text}
        if self.words:
            data["words"] = [w.to_dict() for w in self.words]
        return data

    @classmethod
    def from_dict(cls, d: dict) -> "CaptionSegment":
        return cls(
            start=_ms(d["start"]),
            end=_ms(d["end"]),
            text=str(d.get("text", "")),
            id=str(d.get("id") or _new_id()),
            words=[Word.from_dict(w) for w in d.get("words", [])],
        )


def validate_times(start: float, end: float, duration: float | None = None) -> str | None:
    """Return an error message if the time range is not valid, else ``None``."""
    try:
        start, end = float(start), float(end)
    except (TypeError, ValueError):
        return "Times must be numbers"
    if start != start or end != end:  # NaN
        return "Times must be numbers"
    if start < 0:
        return "Start time cannot be negative"
    if end - start < MIN_SEGMENT_DURATION:
        return f"End time must be at least {int(MIN_SEGMENT_DURATION * 1000)} ms after start time"
    if duration and duration > 0 and start >= duration:
        return "Start time is after the end of the video"
    return None


def clean_text(text: str) -> str:
    """Normalise whitespace while keeping intentional line breaks."""
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in str(text).replace("\r\n", "\n").split("\n")]
    return "\n".join(line for line in lines if line)


def find_overlaps(segments: list[CaptionSegment]) -> list[tuple[str, str]]:
    ordered = sorted(segments, key=lambda s: (s.start, s.end))
    return [(a.id, b.id) for a, b in zip(ordered, ordered[1:]) if b.start < a.end - 1e-6]


@dataclass
class CaptionTrack:
    segments: list[CaptionSegment] = field(default_factory=list)
    language: str | None = None
    source: str = "manual"  # "whisper:<model>", "srt", "manual"
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    # AI correction suggestions awaiting user review: [{id, original, suggested}]
    pending_corrections: list[dict] = field(default_factory=list)

    # -- serialisation -------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "source": self.source,
            "updated_at": self.updated_at,
            "segments": [s.to_dict() for s in self.segments],
            "pending_corrections": self.pending_corrections,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CaptionTrack":
        track = cls(
            segments=[CaptionSegment.from_dict(s) for s in d.get("segments", [])],
            language=d.get("language"),
            source=d.get("source", "manual"),
            updated_at=d.get("updated_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            pending_corrections=list(d.get("pending_corrections", [])),
        )
        track.sort()
        return track

    def copy(self) -> "CaptionTrack":
        return CaptionTrack.from_dict(self.to_dict())

    # -- queries -------------------------------------------------------------
    def sort(self) -> None:
        self.segments.sort(key=lambda s: (s.start, s.end))

    def find(self, segment_id: str) -> CaptionSegment:
        for seg in self.segments:
            if seg.id == segment_id:
                return seg
        raise CaptionError("Caption segment not found")

    def index_of(self, segment_id: str) -> int:
        for i, seg in enumerate(self.segments):
            if seg.id == segment_id:
                return i
        raise CaptionError("Caption segment not found")

    def active_at(self, t: float) -> list[CaptionSegment]:
        """Segments visible at time ``t`` (start inclusive, end exclusive)."""
        starts = [s.start for s in self.segments]
        hi = bisect.bisect_right(starts, t)
        return [s for s in self.segments[:hi] if s.end > t]

    def _touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # -- edits ---------------------------------------------------------------
    def add(self, start: float, end: float | None = None, text: str = "",
            video_duration: float | None = None) -> CaptionSegment:
        """Insert a segment. If ``end`` is omitted, use up to 2 s, stopping at the next caption."""
        start = _ms(start)
        automatic_end = end is None
        if end is None:
            end = start + DEFAULT_NEW_SEGMENT_DURATION
            following = [s.start for s in self.segments if s.start > start]
            if following:
                end = min(end, min(following))
            if video_duration:
                end = min(end, video_duration)
        end = _ms(end)
        error = validate_times(start, end, video_duration)
        if error:
            if automatic_end:
                error = "Not enough room for a new caption here (the next caption starts too soon)"
            raise CaptionError(error)
        seg = CaptionSegment(start=start, end=end, text=clean_text(text))
        self.segments.append(seg)
        self.sort()
        self._touch()
        return seg

    def delete(self, segment_ids: set[str]) -> int:
        before = len(self.segments)
        self.segments = [s for s in self.segments if s.id not in segment_ids]
        removed = before - len(self.segments)
        if removed:
            self._touch()
        return removed

    def set_text(self, segment_id: str, text: str) -> None:
        seg = self.find(segment_id)
        cleaned = clean_text(text)
        if cleaned != seg.text:
            seg.text = cleaned
            seg.words = []  # word timings no longer match edited text
            self._touch()

    def set_times(self, segment_id: str, start: float, end: float, video_duration: float | None = None) -> None:
        error = validate_times(start, end, video_duration)
        if error:
            raise CaptionError(error)
        seg = self.find(segment_id)
        seg.start, seg.end = _ms(start), _ms(end)
        seg.words = [w for w in seg.words if w.end > seg.start and w.start < seg.end]
        self.sort()
        self._touch()

    def split(self, segment_id: str, at_time: float | None = None, text_index: int | None = None) -> tuple[CaptionSegment, CaptionSegment]:
        """Split a segment in two.

        - With word timings and ``at_time``: words starting before ``at_time`` stay in the first part.
        - With ``text_index``: text is cut at that character; the time is placed proportionally
          (or at ``at_time`` if given).
        - Otherwise: cut at the word boundary nearest the time-proportional position.
        """
        seg = self.find(segment_id)
        if at_time is not None and not (seg.start < at_time < seg.end):
            raise CaptionError("The split point must be inside the caption")

        first_words: list[Word] = []
        second_words: list[Word] = []
        if seg.words and at_time is not None and text_index is None:
            first_words = [w for w in seg.words if w.start < at_time]
            second_words = [w for w in seg.words if w.start >= at_time]
            if not first_words or not second_words:
                raise CaptionError("There are no words on one side of the split point")
            text1 = clean_text("".join(w.text for w in first_words))
            text2 = clean_text("".join(w.text for w in second_words))
            cut = _ms(at_time)
        else:
            text = seg.text
            if text_index is None:
                ratio = (at_time - seg.start) / (seg.end - seg.start) if at_time is not None else 0.5
                text_index = _nearest_break(text, int(round(len(text) * ratio)))
            if not (0 < text_index < len(text)):
                raise CaptionError("Place the cursor inside the text to split it")
            text1, text2 = clean_text(text[:text_index]), clean_text(text[text_index:])
            if not text1 or not text2:
                raise CaptionError("Both parts of a split need text")
            if at_time is not None:
                cut = _ms(at_time)
            else:
                cut = _ms(seg.start + (seg.end - seg.start) * (text_index / len(text)))
            if seg.words:
                first_words = [w for w in seg.words if w.start < cut]
                second_words = [w for w in seg.words if w.start >= cut]

        if cut - seg.start < MIN_SEGMENT_DURATION or seg.end - cut < MIN_SEGMENT_DURATION:
            raise CaptionError("The split point is too close to the start or end of the caption")

        index = self.index_of(segment_id)
        a = CaptionSegment(start=seg.start, end=cut, text=text1, id=seg.id, words=first_words)
        b = CaptionSegment(start=cut, end=seg.end, text=text2, words=second_words)
        self.segments[index:index + 1] = [a, b]
        self.sort()
        self._touch()
        return a, b

    def merge(self, segment_ids: list[str] | set[str]) -> CaptionSegment:
        """Merge two or more segments that are next to each other in time order."""
        indexes = sorted(self.index_of(i) for i in set(segment_ids))
        if len(indexes) < 2:
            raise CaptionError("Select at least two captions to merge")
        if indexes != list(range(indexes[0], indexes[-1] + 1)):
            raise CaptionError("Only neighbouring captions can be merged")
        parts = [self.segments[i] for i in indexes]
        merged = CaptionSegment(
            start=min(p.start for p in parts),
            end=max(p.end for p in parts),
            text=clean_text(" ".join(p.text for p in parts)),
            id=parts[0].id,
            words=[w for p in parts for w in p.words] if all(p.words for p in parts) else [],
        )
        self.segments[indexes[0]:indexes[-1] + 1] = [merged]
        self._touch()
        return merged


def _nearest_break(text: str, index: int) -> int:
    """Nearest whitespace position to ``index`` (so words are not cut in half)."""
    spaces = [i for i, ch in enumerate(text) if ch.isspace()]
    if not spaces:
        return index
    return min(spaces, key=lambda i: abs(i - index))


# -- re-segmentation ----------------------------------------------------------

def segments_from_words(words: list[Word], max_chars: int = 42, max_duration: float = 6.0,
                        max_gap: float = 1.0) -> list[CaptionSegment]:
    """Group word timings into readable captions.

    A new caption starts when adding the next word would exceed ``max_chars`` or
    ``max_duration``, after a pause longer than ``max_gap``, or after sentence-ending
    punctuation once the caption is at least half full.
    """
    segments: list[CaptionSegment] = []
    current: list[Word] = []

    def flush() -> None:
        if current:
            text = clean_text("".join(w.text for w in current))
            if text:
                segments.append(CaptionSegment(start=_ms(current[0].start), end=_ms(current[-1].end),
                                               text=text, words=list(current)))
            current.clear()

    for word in words:
        if not word.text.strip():
            continue
        if current:
            candidate = clean_text("".join(w.text for w in current) + word.text)
            prev = current[-1]
            if (len(candidate) > max_chars
                    or word.end - current[0].start > max_duration
                    or word.start - prev.end > max_gap
                    or (prev.text.rstrip()[-1:] in ".?!।" and len(candidate) > max_chars // 2)):
                flush()
        current.append(word)
    flush()
    # Ensure strictly valid, non-overlapping times.
    for a, b in zip(segments, segments[1:]):
        if b.start < a.end:
            a.end = b.start
    return [s for s in segments if s.end - s.start >= MIN_SEGMENT_DURATION]


# -- cut remapping --------------------------------------------------------------

def remap_to_cuts(segments: list[CaptionSegment], keep_ranges: list[tuple[float, float]]) -> list[CaptionSegment]:
    """Map caption times from the source timeline onto an edited timeline.

    ``keep_ranges`` are the parts of the source that remain, in order; everything else is
    cut out and the kept parts are joined. Captions entirely inside removed parts are
    dropped; partly removed captions are trimmed. When word timings exist, words that were
    cut out are removed from the text as well.
    """
    ranges = sorted((float(a), float(b)) for a, b in keep_ranges if b > a)
    for (a1, b1), (a2, _b2) in zip(ranges, ranges[1:]):
        if a2 < b1:
            raise CaptionError("Keep ranges must not overlap")
    offsets = []
    total = 0.0
    for a, b in ranges:
        offsets.append(total)
        total += b - a

    def map_time(t: float) -> float | None:
        for (a, b), off in zip(ranges, offsets):
            if a <= t <= b:
                return off + (t - a)
        return None

    def clip(start: float, end: float) -> tuple[float, float] | None:
        mapped = []
        for (a, b), off in zip(ranges, offsets):
            s, e = max(start, a), min(end, b)
            if e > s:
                mapped.append((off + s - a, off + e - a))
        if not mapped:
            return None
        return mapped[0][0], mapped[-1][1]

    result = []
    for seg in segments:
        span = clip(seg.start, seg.end)
        if span is None or span[1] - span[0] < MIN_SEGMENT_DURATION:
            continue
        words = []
        for w in seg.words:
            mid = (w.start + w.end) / 2
            if map_time(mid) is None:
                continue
            ws = clip(w.start, w.end)
            if ws:
                words.append(Word(_ms(ws[0]), _ms(ws[1]), w.text))
        text = seg.text
        if seg.words:
            if not words:
                continue
            text = clean_text("".join(w.text for w in words))
        result.append(CaptionSegment(start=_ms(span[0]), end=_ms(span[1]), text=text, id=seg.id, words=words))
    return result


# -- time entry ---------------------------------------------------------------

def format_clock(seconds: float) -> str:
    """``75.5`` → ``01:15.500``; ``3725.5`` → ``1:02:05.500``."""
    total_ms = int(round(max(0.0, seconds) * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}.{ms:03d}"
    return f"{minutes:02d}:{secs:02d}.{ms:03d}"


def parse_clock(text: str) -> float:
    """Parse ``ss.mmm``, ``mm:ss.mmm`` or ``hh:mm:ss.mmm`` (``,`` also accepted). Raises CaptionError."""
    value = text.strip().replace(",", ".")
    parts = value.split(":")
    try:
        if not value or len(parts) > 3 or any(p.strip() == "" for p in parts):
            raise ValueError
        seconds = float(parts[-1])
        minutes = int(parts[-2]) if len(parts) >= 2 else 0
        hours = int(parts[-3]) if len(parts) == 3 else 0
        if seconds < 0 or minutes < 0 or hours < 0 or (len(parts) >= 2 and (seconds >= 60 or minutes >= 60 and len(parts) == 3)):
            raise ValueError
    except ValueError:
        raise CaptionError(f"'{text}' is not a valid time (use mm:ss.mmm)") from None
    return _ms(hours * 3600 + minutes * 60 + seconds)

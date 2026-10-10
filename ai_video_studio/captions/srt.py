"""SRT subtitle parsing and writing."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ai_video_studio.captions.model import CaptionSegment, CaptionTrack, clean_text, validate_times
from ai_video_studio.core.errors import StudioError

_TIME = r"(\d{1,3}):(\d{1,2}):(\d{1,2})(?:[,.](\d{1,3}))?"
_TIMING_LINE = re.compile(rf"^\s*{_TIME}\s*-->\s*{_TIME}")
_TAGS = re.compile(r"</?(?:b|i|u|font)[^>]*>|\{\\[^}]*\}", re.IGNORECASE)


class SrtError(StudioError):
    pass


@dataclass
class SrtParseResult:
    segments: list[CaptionSegment]
    warnings: list[str] = field(default_factory=list)


def format_timestamp(seconds: float) -> str:
    """``3725.5`` → ``01:02:05,500``."""
    if seconds < 0:
        raise ValueError("negative timestamp")
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def parse_timestamp(text: str) -> float:
    match = re.fullmatch(_TIME, text.strip())
    if not match:
        raise ValueError(f"invalid timestamp {text!r}")
    return _to_seconds(match.groups())


def _to_seconds(parts) -> float:
    h, m, s, ms = parts
    if int(m) > 59 or int(s) > 59:
        raise ValueError("minutes/seconds out of range")
    frac = int((ms or "0").ljust(3, "0")[:3])
    return round(int(h) * 3600 + int(m) * 60 + int(s) + frac / 1000, 3)


def parse_srt(text: str) -> SrtParseResult:
    """Parse SRT text. Malformed blocks are skipped and reported as warnings.

    Accepts a UTF-8 BOM, CRLF/CR line endings, missing or wrong index numbers,
    ``.`` instead of ``,`` before milliseconds, multi-line text and basic
    formatting tags (which are removed).
    """
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", text.strip())
    segments: list[CaptionSegment] = []
    warnings: list[str] = []
    for number, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        timing_at = next((i for i, line in enumerate(lines[:3]) if "-->" in line), None)
        if timing_at is None:
            if block.strip():
                warnings.append(f"Block {number}: no timing line, skipped")
            continue
        match = _TIMING_LINE.match(lines[timing_at])
        if not match:
            warnings.append(f"Block {number}: unreadable timing '{lines[timing_at].strip()}', skipped")
            continue
        try:
            start = _to_seconds(match.groups()[:4])
            end = _to_seconds(match.groups()[4:])
        except ValueError as exc:
            warnings.append(f"Block {number}: {exc}, skipped")
            continue
        body = clean_text(_TAGS.sub("", "\n".join(lines[timing_at + 1:])))
        error = validate_times(start, end)
        if error:
            warnings.append(f"Block {number}: {error}, skipped")
            continue
        if not body:
            warnings.append(f"Block {number}: empty text, skipped")
            continue
        segments.append(CaptionSegment(start=start, end=end, text=body))
    segments.sort(key=lambda s: (s.start, s.end))
    return SrtParseResult(segments, warnings)


def to_srt(segments: list[CaptionSegment]) -> str:
    ordered = sorted(segments, key=lambda s: (s.start, s.end))
    blocks = []
    for index, seg in enumerate(ordered, start=1):
        blocks.append(f"{index}\n{format_timestamp(seg.start)} --> {format_timestamp(seg.end)}\n{seg.text}\n")
    return "\n".join(blocks)


def read_srt_file(path: str | Path) -> SrtParseResult:
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - cp1252 decodes almost anything
        raise SrtError("Could not decode the subtitle file")
    result = parse_srt(text)
    if not result.segments:
        raise SrtError("No valid captions were found in the file.")
    return result


def write_srt_file(path: str | Path, track: CaptionTrack) -> Path:
    path = Path(path)
    if path.suffix.lower() != ".srt":
        path = path.with_suffix(".srt")
    path.write_text(to_srt(track.segments), encoding="utf-8")
    return path

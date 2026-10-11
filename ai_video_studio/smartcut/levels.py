"""Audio levels per short window, measured by FFmpeg's ``astats`` filter.

The audio is analysed once; silence detection then runs on these levels in Python, so
changing the threshold or padding updates the proposal instantly without decoding again.
``Peak_level`` decides silence (like FFmpeg's ``silencedetect``: a window is quiet only if no
sample is louder than the threshold) and ``Overall`` combines all channels, so a window
counts as silent only when every channel is quiet.
"""

from __future__ import annotations

import base64
import math
from dataclasses import dataclass
from pathlib import Path

WINDOW = 0.025          # seconds per level value
ANALYSIS_RATE = 16000   # samples/s used for the measurement
FLOOR_DB = -120         # digital silence ("-inf") and the lowest stored value


def levels_args(ffmpeg: Path | str, source: str) -> list[str]:
    samples = int(round(WINDOW * ANALYSIS_RATE))
    chain = (f"aresample={ANALYSIS_RATE},asetnsamples=n={samples}:p=0,"
             "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=Peak_level+RMS_level,"
             "ametadata=mode=print:file=-")
    return [str(ffmpeg), "-hide_banner", "-nostdin", "-v", "error", "-i", str(source),
            "-map", "0:a:0", "-vn", "-sn", "-dn", "-af", chain, "-f", "null", "-"]


def _db(value: str) -> int:
    try:
        number = float(value)
    except ValueError:
        return FLOOR_DB
    if math.isnan(number) or number < FLOOR_DB:
        return FLOOR_DB
    return int(round(min(0.0, number)))


class LevelsParser:
    """Collects ``ametadata`` output lines into per-window peak and RMS levels (dBFS)."""

    def __init__(self) -> None:
        self.peak: list[int] = []
        self.rms: list[int] = []
        self._time: float | None = None
        self.last_time = 0.0

    def feed(self, line: str) -> float | None:
        """Returns the time of a new window (for progress), else None."""
        line = line.strip()
        if line.startswith("frame:"):
            for part in line.split():
                if part.startswith("pts_time:"):
                    try:
                        self._time = float(part[9:])
                    except ValueError:
                        self._time = None
            return None
        key, _, value = line.partition("=")
        if key.endswith("Overall.Peak_level"):
            self._place(self.peak, _db(value))
            self.last_time = self._time or self.last_time
            return self.last_time
        if key.endswith("Overall.RMS_level"):
            self._place(self.rms, _db(value))
        return None

    def _place(self, values: list[int], db: int) -> None:
        # Windows before the first audio packet (audio that starts late) count as silence.
        index = int(round((self._time or 0.0) / WINDOW)) if self._time is not None else len(values)
        while len(values) < index:
            values.append(FLOOR_DB)
        if len(values) == index:
            values.append(db)
        else:  # repeated timestamp: keep the louder value
            values[index] = max(values[index], db)


def _pack(values: list[int]) -> str:
    return base64.b64encode(bytes(max(0, min(255, -v)) for v in values)).decode("ascii")


def _unpack(text: str) -> list[int]:
    return [-b for b in base64.b64decode(text.encode("ascii"))]


@dataclass
class AudioLevels:
    peak: list[int]
    rms: list[int]
    duration: float
    window: float = WINDOW
    source_size: int | None = None
    source_mtime: float | None = None

    def __post_init__(self) -> None:
        if len(self.rms) < len(self.peak):
            self.rms = self.rms + [FLOOR_DB] * (len(self.peak) - len(self.rms))

    def level_at(self, t: float) -> int:
        i = int(t / self.window)
        return self.peak[i] if 0 <= i < len(self.peak) else FLOOR_DB

    def to_dict(self) -> dict:
        return {"window": self.window, "duration": self.duration, "peak": _pack(self.peak), "rms": _pack(self.rms),
                "source_size": self.source_size, "source_mtime": self.source_mtime}

    @classmethod
    def from_dict(cls, d: dict) -> "AudioLevels":
        return cls(peak=_unpack(d.get("peak", "")), rms=_unpack(d.get("rms", "")), duration=float(d["duration"]),
                   window=float(d.get("window", WINDOW)), source_size=d.get("source_size"),
                   source_mtime=d.get("source_mtime"))


def percentile(values: list[int], fraction: float) -> float:
    if not values:
        return float(FLOOR_DB)
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, max(0, int(fraction * (len(ordered) - 1))))])


def suggest_threshold(levels: AudioLevels) -> tuple[float, str]:
    """A threshold between the background level and the speech level, with an explanation.

    The background level is the 10th percentile of the window peaks (ignoring digital
    silence) and the speech level the 90th. A small difference between the two means the
    quiet parts are not really quiet (music, room noise), and silence removal may cut words.
    """
    audible = [v for v in levels.peak if v > -100]
    if len(audible) < 10:
        return -35.0, "The audio is almost completely silent."
    floor, loud = percentile(audible, 0.10), percentile(audible, 0.90)
    spread = loud - floor
    threshold = floor + max(6.0, spread * 0.3)
    threshold = round(max(-70.0, min(-15.0, min(threshold, loud - 8.0))))
    message = f"Background about {floor:.0f} dB, speech peaks about {loud:.0f} dB."
    if spread < 12:
        message += (" The quiet parts are not much quieter than the rest (background music or noise?), "
                    "so detected silences may contain sound. Review the proposal carefully.")
    return float(threshold), message

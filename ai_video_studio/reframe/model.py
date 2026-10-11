"""Data model for tracking results and reframe settings.

Coordinates in detections and tracks are normalised to the displayed frame (after
rotation): ``x``/``y`` is the top-left corner and ``w``/``h`` the size, all in 0..1.
This keeps analysis results independent of the analysis resolution and the preview size.
"""

from __future__ import annotations

import bisect
from dataclasses import asdict, dataclass, field, fields

from ai_video_studio.core.errors import StudioError

KINDS = ("face", "person")
MODES = ("track", "center")
CAPTION_SAFE = ("off", "bottom", "top")


class ReframeError(StudioError):
    pass


@dataclass(frozen=True)
class Detection:
    x: float
    y: float
    w: float
    h: float
    confidence: float
    kind: str = "face"

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


@dataclass(frozen=True)
class TrackSample:
    t: float
    x: float
    y: float
    w: float
    h: float
    confidence: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def to_list(self) -> list:
        return [round(self.t, 3), round(self.x, 4), round(self.y, 4), round(self.w, 4), round(self.h, 4),
                round(self.confidence, 3)]

    @classmethod
    def from_list(cls, row: list) -> "TrackSample":
        t, x, y, w, h, c = (float(v) for v in row[:6])
        return cls(t, x, y, w, h, c)


@dataclass
class Track:
    """One subject followed over time. ``samples`` are sorted by time."""

    id: int
    kind: str
    samples: list[TrackSample] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._times = [s.t for s in self.samples]

    @property
    def start(self) -> float:
        return self.samples[0].t if self.samples else 0.0

    @property
    def end(self) -> float:
        return self.samples[-1].t if self.samples else 0.0

    @property
    def mean_confidence(self) -> float:
        return sum(s.confidence for s in self.samples) / len(self.samples) if self.samples else 0.0

    @property
    def mean_area(self) -> float:
        return sum(s.w * s.h for s in self.samples) / len(self.samples) if self.samples else 0.0

    def box_at(self, t: float, tolerance: float, max_gap: float) -> TrackSample | None:
        """The subject's box at time ``t``.

        Between two samples less than ``max_gap`` apart (consecutive detections or a brief
        occlusion) the box is linearly interpolated. Otherwise the nearest sample within
        ``tolerance`` seconds is used (start and end of a track), or ``None`` when the
        subject was not seen around ``t``.
        """
        if not self.samples:
            return None
        i = bisect.bisect_left(self._times, t)
        before = self.samples[i - 1] if i > 0 else None
        after = self.samples[i] if i < len(self.samples) else None
        if after is not None and after.t == t:
            return after
        if before is not None and after is not None and after.t - before.t <= max_gap:
            k = (t - before.t) / (after.t - before.t)
            mix = lambda a, b: a + (b - a) * k  # noqa: E731
            return TrackSample(t, mix(before.x, after.x), mix(before.y, after.y), mix(before.w, after.w),
                               mix(before.h, after.h), min(before.confidence, after.confidence))
        nearest = min((s for s in (before, after) if s is not None), key=lambda s: abs(s.t - t))
        return nearest if abs(nearest.t - t) <= tolerance else None

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "samples": [s.to_list() for s in self.samples]}

    @classmethod
    def from_dict(cls, data: dict) -> "Track":
        return cls(id=int(data["id"]), kind=str(data.get("kind", "face")),
                   samples=[TrackSample.from_list(r) for r in data.get("samples", [])])


@dataclass
class TrackingAnalysis:
    """Result of analysing one video. Cached in the project so it is not recomputed."""

    width: int                 # displayed source size in pixels
    height: int
    duration: float
    sample_fps: float          # frames analysed per second of video
    frames_analyzed: int
    detector: str              # human-readable description of the detectors used
    tracks: list[Track] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_size: int | None = None    # file size and mtime when analysed (to detect a changed file)
    source_mtime: float | None = None

    def track(self, track_id: int) -> Track | None:
        return next((t for t in self.tracks if t.id == track_id), None)

    @property
    def sample_interval(self) -> float:
        return 1.0 / self.sample_fps if self.sample_fps > 0 else 1.0

    def coverage(self, track: Track) -> float:
        """Fraction of analysed frames in which the track was detected."""
        return min(1.0, len(track.samples) / self.frames_analyzed) if self.frames_analyzed else 0.0

    def to_dict(self) -> dict:
        return {
            "width": self.width, "height": self.height, "duration": round(self.duration, 3),
            "sample_fps": self.sample_fps, "frames_analyzed": self.frames_analyzed, "detector": self.detector,
            "tracks": [t.to_dict() for t in self.tracks], "warnings": list(self.warnings),
            "source_size": self.source_size, "source_mtime": self.source_mtime,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TrackingAnalysis":
        try:
            return cls(
                width=int(data["width"]), height=int(data["height"]), duration=float(data["duration"]),
                sample_fps=float(data["sample_fps"]), frames_analyzed=int(data.get("frames_analyzed", 0)),
                detector=str(data.get("detector", "")),
                tracks=[Track.from_dict(t) for t in data.get("tracks", [])],
                warnings=[str(w) for w in data.get("warnings", [])],
                source_size=data.get("source_size"), source_mtime=data.get("source_mtime"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReframeError(f"Tracking data is damaged: {exc}") from exc


@dataclass
class ReframeSettings:
    """User choices for one video. Defaults suit talking-head footage."""

    mode: str = "track"              # track | center
    preset: str = "9:16"             # see framing.ASPECT_PRESETS
    output_width: int = 0            # 0 = default size for the preset
    output_height: int = 0
    target: int | None = None        # track id to follow; None = automatic choice
    switches: list[list] = field(default_factory=list)  # [[time, track_id], ...] subject changes
    zoom: float = 1.0                # 1 = largest crop that fits; 2 = half the size
    padding: float = 0.15            # margin kept around the subject, as a fraction of its size
    subject_y: float = 0.4           # where the subject's centre sits vertically in the crop (0 top, 1 bottom)
    offset_x: float = 0.0            # manual shift of the crop, fraction of crop width (+ = right)
    offset_y: float = 0.0            # manual shift of the crop, fraction of crop height (+ = down)
    smoothing: float = 0.5           # 0..1: temporal smoothing strength (zero-lag, both directions)
    follow_speed: float = 0.8        # max camera speed in crop widths per second
    dead_zone: float = 0.05          # movement below this fraction of the crop is ignored
    hold_seconds: float = 1.0        # keep framing this long after the subject is lost
    transition_seconds: float = 0.8  # length of a controlled pan when the subject changes or reappears
    caption_safe: str = "bottom"     # off | bottom | top
    caption_safe_height: float = 0.25  # fraction of the output height reserved for captions
    burn_captions: bool = True
    analysis_fps: float = 5.0        # frames analysed per second of video
    detect_persons: bool = True      # look for full bodies in frames without faces

    def validate(self) -> None:
        from ai_video_studio.reframe.framing import ASPECT_PRESETS

        errors = []
        if self.mode not in MODES:
            errors.append("Mode must be 'track' or 'center'")
        if self.preset not in ASPECT_PRESETS:
            errors.append(f"Unknown aspect preset '{self.preset}'")
        if self.output_width < 0 or self.output_height < 0 or (self.output_width == 0) != (self.output_height == 0):
            errors.append("Output size must be both set or both automatic")
        if self.output_width and (self.output_width < 64 or self.output_height < 64
                                  or self.output_width > 7680 or self.output_height > 7680):
            errors.append("Output size must be between 64 and 7680 pixels")
        ranges = {
            "zoom": (1.0, 4.0), "padding": (0.0, 1.0), "subject_y": (0.1, 0.9), "offset_x": (-0.5, 0.5),
            "offset_y": (-0.5, 0.5), "smoothing": (0.0, 1.0), "follow_speed": (0.05, 5.0),
            "dead_zone": (0.0, 0.4), "hold_seconds": (0.0, 10.0), "transition_seconds": (0.0, 5.0),
            "caption_safe_height": (0.05, 0.5), "analysis_fps": (0.5, 30.0),
        }
        for name, (low, high) in ranges.items():
            value = getattr(self, name)
            if not low <= value <= high:
                errors.append(f"{name.replace('_', ' ').capitalize()} must be between {low} and {high}")
        if self.caption_safe not in CAPTION_SAFE:
            errors.append("Caption area must be off, bottom or top")
        for row in self.switches:
            if len(row) != 2 or float(row[0]) < 0:
                errors.append("Subject changes are damaged")
                break
        if errors:
            raise ReframeError("; ".join(errors))

    def subject_schedule(self) -> list[tuple[float, int | None]]:
        """``[(start_time, track_id or None for automatic), ...]`` sorted by time."""
        schedule = [(0.0, self.target)]
        for t, track_id in sorted(self.switches, key=lambda r: float(r[0])):
            schedule.append((float(t), None if track_id is None else int(track_id)))
        return schedule

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ReframeSettings":
        known = {f.name for f in fields(cls)}
        settings = cls(**{k: v for k, v in data.items() if k in known})
        settings.validate()
        return settings


@dataclass
class ReframeState:
    """Per-video reframe data stored in the project file."""

    settings: ReframeSettings = field(default_factory=ReframeSettings)
    analysis: TrackingAnalysis | None = None

    def to_dict(self) -> dict:
        return {"settings": self.settings.to_dict(),
                "analysis": self.analysis.to_dict() if self.analysis else None}

    @classmethod
    def from_dict(cls, data: dict) -> "ReframeState":
        analysis = data.get("analysis")
        return cls(settings=ReframeSettings.from_dict(data.get("settings") or {}),
                   analysis=TrackingAnalysis.from_dict(analysis) if analysis else None)

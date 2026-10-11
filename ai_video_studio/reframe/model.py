"""Data model for tracking results and reframe settings.

Coordinates in detections and tracks are normalised to the displayed frame (after
rotation): ``x``/``y`` is the top-left corner and ``w``/``h`` the size, all in 0..1.
This keeps analysis results independent of the analysis resolution and the preview size.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import asdict, dataclass, field, fields

from ai_video_studio.core.errors import StudioError

KINDS = ("face", "person")
CAPTION_SAFE = ("auto", "off", "bottom", "top")  # auto = derived from the video's caption style
FALLBACKS = ("hold", "center", "group")
ANALYSIS_FORMAT = 2  # 1 = Phase 3 (v0.3.0); 2 adds features, mouth movement, speech and appearance


@dataclass(frozen=True)
class ModeInfo:
    label: str
    description: str
    requires: frozenset          # analysis features this mode needs
    uses_subjects: bool = False  # subject list / manual choice
    uses_speech: bool = False    # speech-activity threshold
    uses_speaker: bool = False   # speaker-switch confirmation


TRACKING_MODES: dict[str, ModeInfo] = {
    "auto_face": ModeInfo(
        "Auto Face Tracking",
        "Follows one face: the one on screen longest, or the one you choose. It never switches to "
        "another person because their face is bigger. No audio is used.",
        frozenset({"faces"}), uses_subjects=True),
    "active_speaker": ModeInfo(
        "Face + Speech Audio (Active Speaker)",
        "Estimates who is talking by matching each visible person's mouth movement with the speech "
        "in the audio, and follows that person. Switches only after the evidence has lasted for the "
        "confirmation time. When nobody visible can be matched (off-screen voice, overlapping "
        "speech), it keeps the last speaker briefly, then uses the fallback.",
        frozenset({"faces", "mouth", "speech"}), uses_speech=True, uses_speaker=True),
    "speech_audio": ModeInfo(
        "Speech-Audio-Driven",
        "Uses speech timing from the audio: the framing only changes while someone is speaking and "
        "holds still during silence. Audio alone cannot tell which visible person is speaking, so "
        "with one visible person it follows them, and with several it does not guess: it uses the "
        "fallback instead.",
        frozenset({"speech"}), uses_speech=True),
    "manual": ModeInfo(
        "Manual Subject Tracking",
        "Follows exactly the subject you choose (face or full body), with optional changes from the "
        "playhead. Nothing is chosen automatically.",
        frozenset({"faces"}), uses_subjects=True),
    "auto_visible": ModeInfo(
        "Automatic Visible Subject",
        "Follows the best visible subject (faces first, then full bodies). When it is lost for "
        "longer than the hold time, it moves to the best subject still visible.",
        frozenset({"faces"}), uses_subjects=True),
    "center": ModeInfo(
        "Center Crop",
        "A fixed crop, positioned with the offsets. No analysis or tracking.",
        frozenset()),
}
MODES = tuple(TRACKING_MODES)


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
    # Optional extras from the detector, normalised like the box: five points (right eye, left
    # eye, nose tip, right and left mouth corner) as (x0, y0, ..., x4, y4).
    landmarks: tuple[float, ...] | None = None
    appearance: tuple[float, ...] | None = None  # normalised colour histogram (identity evidence)

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
    mouth: float | None = None  # mouth-region movement since the previous analysed frame (None = unknown)

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def to_list(self) -> list:
        row = [round(self.t, 3), round(self.x, 4), round(self.y, 4), round(self.w, 4), round(self.h, 4),
               round(self.confidence, 3)]
        if self.mouth is not None:
            row.append(round(self.mouth, 4))
        return row

    @classmethod
    def from_list(cls, row: list) -> "TrackSample":
        t, x, y, w, h, c = (float(v) for v in row[:6])
        mouth = float(row[6]) if len(row) > 6 and row[6] is not None else None
        return cls(t, x, y, w, h, c, mouth)


@dataclass
class Track:
    """One subject followed over time. ``samples`` are sorted by time."""

    id: int
    kind: str
    samples: list[TrackSample] = field(default_factory=list)
    appearance: list[float] | None = None  # mean colour histogram, used to re-link after occlusion

    def __post_init__(self) -> None:
        self._times = [s.t for s in self.samples]

    @property
    def has_mouth(self) -> bool:
        return any(s.mouth is not None for s in self.samples)

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
        data = {"id": self.id, "kind": self.kind, "samples": [s.to_list() for s in self.samples]}
        if self.appearance is not None:
            data["appearance"] = [round(v, 4) for v in self.appearance]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Track":
        appearance = data.get("appearance")
        return cls(id=int(data["id"]), kind=str(data.get("kind", "face")),
                   samples=[TrackSample.from_list(r) for r in data.get("samples", [])],
                   appearance=[float(v) for v in appearance] if appearance else None)


@dataclass
class SpeechActivity:
    """Speech evidence from the audio track, sampled at ``rate`` values per second from time 0.

    ``prob`` is the voice-activity probability (Silero VAD) and ``energy`` the loudness envelope
    (0..1). Voice activity says *that* someone speaks, not *who*: identifying the speaker needs
    visual evidence (see ``reframe.speaker``).
    """

    rate: float
    prob: list[float] = field(default_factory=list)
    energy: list[float] = field(default_factory=list)
    detector: str = ""

    @property
    def duration(self) -> float:
        return len(self.prob) / self.rate if self.rate > 0 else 0.0

    def _index(self, t: float) -> int | None:
        if not self.prob or t < 0:
            return None
        i = int(t * self.rate)
        return i if i < len(self.prob) else None

    def prob_at(self, t: float) -> float:
        i = self._index(t)
        return self.prob[i] if i is not None else 0.0

    def energy_at(self, t: float) -> float:
        i = self._index(t)
        return self.energy[i] if i is not None and i < len(self.energy) else 0.0

    def speech_fraction(self, t0: float, t1: float, threshold: float) -> float:
        """Share of ``[t0, t1)`` with speech probability at or above ``threshold``."""
        a, b = max(0, int(t0 * self.rate)), min(len(self.prob), int(math.ceil(t1 * self.rate)))
        if b <= a:
            return 0.0
        return sum(1 for p in self.prob[a:b] if p >= threshold) / (b - a)

    def segments(self, threshold: float, min_speech: float = 0.25, min_silence: float = 0.3) -> list[tuple[float, float]]:
        """Speech intervals, ignoring blips shorter than ``min_speech`` and gaps shorter than ``min_silence``."""
        raw: list[list[float]] = []
        start = None
        for i, p in enumerate(self.prob + [0.0]):
            if p >= threshold and start is None:
                start = i
            elif p < threshold and start is not None:
                raw.append([start / self.rate, i / self.rate])
                start = None
        merged: list[list[float]] = []
        for seg in raw:
            if merged and seg[0] - merged[-1][1] < min_silence:
                merged[-1][1] = seg[1]
            else:
                merged.append(seg)
        return [(a, b) for a, b in merged if b - a >= min_speech]

    def to_dict(self) -> dict:
        return {"rate": self.rate, "prob": [round(v, 2) for v in self.prob],
                "energy": [round(v, 3) for v in self.energy], "detector": self.detector}

    @classmethod
    def from_dict(cls, data: dict) -> "SpeechActivity":
        return cls(rate=float(data["rate"]), prob=[float(v) for v in data.get("prob", [])],
                   energy=[float(v) for v in data.get("energy", [])], detector=str(data.get("detector", "")))


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
    features: list[str] = field(default_factory=lambda: ["faces"])  # faces, persons, mouth, speech
    face_threshold: float = 0.6       # lowest detection score kept in this analysis
    speech: SpeechActivity | None = None
    notes: list[str] = field(default_factory=list)  # informational (not warnings)

    def track(self, track_id: int) -> Track | None:
        return next((t for t in self.tracks if t.id == track_id), None)

    @property
    def sample_interval(self) -> float:
        return 1.0 / self.sample_fps if self.sample_fps > 0 else 1.0

    def frames_between(self, t0: float, t1: float) -> int:
        return int(round((t1 - t0) * self.sample_fps)) + 1

    def detection_rate(self, track: Track) -> float:
        """Share of analysed frames in which the track was detected *while it was on screen*
        (between its first and last detection). This measures tracking reliability."""
        if not track.samples:
            return 0.0
        return min(1.0, len(track.samples) / self.frames_between(track.start, track.end))

    def screen_share(self, track: Track) -> float:
        """Share of the video between the track's first and last detection. A subject who is
        only in part of the video has a low share even when tracking is perfect."""
        if not track.samples or self.duration <= 0:
            return 0.0
        return min(1.0, (track.end - track.start + self.sample_interval) / self.duration)

    def coverage(self, track: Track) -> float:
        """Share of all analysed frames in which the track was detected."""
        return min(1.0, len(track.samples) / self.frames_analyzed) if self.frames_analyzed else 0.0

    def has(self, feature: str) -> bool:
        return feature in self.features

    def to_dict(self) -> dict:
        return {
            "format": ANALYSIS_FORMAT,
            "width": self.width, "height": self.height, "duration": round(self.duration, 3),
            "sample_fps": self.sample_fps, "frames_analyzed": self.frames_analyzed, "detector": self.detector,
            "tracks": [t.to_dict() for t in self.tracks], "warnings": list(self.warnings),
            "source_size": self.source_size, "source_mtime": self.source_mtime,
            "features": list(self.features), "face_threshold": self.face_threshold,
            "speech": self.speech.to_dict() if self.speech else None, "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TrackingAnalysis":
        try:
            tracks = [Track.from_dict(t) for t in data.get("tracks", [])]
            features = data.get("features")
            if features is None:  # format 1 (v0.3.0): faces, plus persons when any were found
                features = ["faces"] + (["persons"] if any(t.kind == "person" for t in tracks) else [])
            speech = data.get("speech")
            return cls(
                width=int(data["width"]), height=int(data["height"]), duration=float(data["duration"]),
                sample_fps=float(data["sample_fps"]), frames_analyzed=int(data.get("frames_analyzed", 0)),
                detector=str(data.get("detector", "")), tracks=tracks,
                warnings=[str(w) for w in data.get("warnings", [])],
                source_size=data.get("source_size"), source_mtime=data.get("source_mtime"),
                features=[str(f) for f in features], face_threshold=float(data.get("face_threshold", 0.6)),
                speech=SpeechActivity.from_dict(speech) if speech else None,
                notes=[str(n) for n in data.get("notes", [])],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReframeError(f"Tracking data is damaged: {exc}") from exc


@dataclass
class ReframeSettings:
    """User choices for one video. Defaults suit talking-head footage."""

    tracking_mode: str = "auto_face"  # see TRACKING_MODES
    preset: str = "9:16"             # see framing.ASPECT_PRESETS
    output_width: int = 0            # 0 = default size for the preset
    output_height: int = 0
    target: int | None = None        # track id to follow from the start; None = automatic choice
    switches: list[list] = field(default_factory=list)  # [[time, track_id], ...] subject changes
    zoom: float = 1.0                # 1 = largest crop that fits; 2 = half the size
    max_upscale: float = 2.0         # zoom is limited so the crop is never enlarged more than this
    padding: float = 0.2             # margin kept around the face/upper body, as a fraction of its size
    subject_y: float = 0.38          # where the face sits vertically in the crop (0 top, 1 bottom)
    offset_x: float = 0.0            # manual shift of the crop, fraction of crop width (+ = right)
    offset_y: float = 0.0            # manual shift of the crop, fraction of crop height (+ = down)
    smoothing: float = 0.5           # 0..1: temporal smoothing strength (zero-lag, both directions)
    follow_speed: float = 0.8        # max camera speed in crop widths per second
    dead_zone: float = 0.05          # movement below this fraction of the crop is ignored
    hold_seconds: float = 1.0        # keep framing this long after the subject is lost
    transition_seconds: float = 0.8  # length of a controlled pan when the subject changes or reappears
    fallback: str = "center"         # when nobody can be followed: hold | center | group
    confidence_threshold: float = 0.6  # minimum face/person detection score used for tracking
    speech_threshold: float = 0.5    # minimum voice-activity probability counted as speech
    switch_seconds: float = 1.0      # active speaker: how long new evidence must last before switching
    speaker_hold_seconds: float = 3.0  # active speaker: keep the last speaker this long when uncertain
    caption_safe: str = "auto"       # auto (from caption style) | off | bottom | top
    caption_safe_height: float = 0.25  # fraction of the output height reserved (bottom/top only)
    burn_captions: bool = True
    analysis_fps: float = 5.0        # frames analysed per second (face tracking)
    av_fps: float = 15.0             # frames analysed per second when mouth movement is needed
    detect_persons: bool = True      # look for full bodies in frames without faces

    @property
    def mode_info(self) -> ModeInfo:
        return TRACKING_MODES[self.tracking_mode]

    @property
    def tracks_subjects(self) -> bool:
        return self.tracking_mode != "center"

    def required_features(self) -> set[str]:
        needed = set(self.mode_info.requires)
        if self.tracking_mode in ("manual", "auto_visible") and self.detect_persons:
            needed.add("persons")
        return needed

    def analysis_features(self) -> list[str]:
        """Features to compute when analysing for this mode."""
        if self.tracking_mode == "center":
            return []
        features = {"faces"} | self.required_features()
        if self.tracking_mode == "speech_audio":
            features.add("faces")  # visual targets for speech timing
        if self.detect_persons and self.tracking_mode != "active_speaker":
            features.add("persons")
        return sorted(features)

    def analysis_rate(self) -> float:
        return max(self.analysis_fps, self.av_fps) if "mouth" in self.required_features() else self.analysis_fps

    def validate(self) -> None:
        from ai_video_studio.reframe.framing import ASPECT_PRESETS

        errors = []
        if self.tracking_mode not in TRACKING_MODES:
            errors.append(f"Unknown tracking mode '{self.tracking_mode}'")
        if self.preset not in ASPECT_PRESETS:
            errors.append(f"Unknown aspect preset '{self.preset}'")
        if self.output_width < 0 or self.output_height < 0 or (self.output_width == 0) != (self.output_height == 0):
            errors.append("Output size must be both set or both automatic")
        if self.output_width and (self.output_width < 64 or self.output_height < 64
                                  or self.output_width > 7680 or self.output_height > 7680):
            errors.append("Output size must be between 64 and 7680 pixels")
        ranges = {
            "zoom": (1.0, 4.0), "max_upscale": (1.0, 4.0), "padding": (0.0, 1.0), "subject_y": (0.1, 0.9),
            "offset_x": (-0.5, 0.5), "offset_y": (-0.5, 0.5), "smoothing": (0.0, 1.0),
            "follow_speed": (0.05, 5.0), "dead_zone": (0.0, 0.4), "hold_seconds": (0.0, 10.0),
            "transition_seconds": (0.0, 5.0), "confidence_threshold": (0.3, 0.99),
            "speech_threshold": (0.1, 0.95), "switch_seconds": (0.0, 5.0), "speaker_hold_seconds": (0.0, 15.0),
            "caption_safe_height": (0.05, 0.5), "analysis_fps": (0.5, 30.0), "av_fps": (5.0, 30.0),
        }
        for name, (low, high) in ranges.items():
            value = getattr(self, name)
            if not low <= value <= high:
                errors.append(f"{name.replace('_', ' ').capitalize()} must be between {low} and {high}")
        if self.caption_safe not in CAPTION_SAFE:
            errors.append("Caption area must be auto, off, bottom or top")
        if self.fallback not in FALLBACKS:
            errors.append("Fallback must be hold, center or group")
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
        data = dict(data)
        if "tracking_mode" not in data and "mode" in data:
            # Project format 3 (v0.3.0): mode track|center, with an optional fixed target.
            legacy = data.get("mode")
            data["tracking_mode"] = ("center" if legacy == "center"
                                     else "manual" if data.get("target") is not None else "auto_visible")
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


FEATURE_LABELS = {"faces": "face tracking", "persons": "full-body detection",
                  "mouth": "mouth movement", "speech": "speech activity from the audio"}


def reanalysis_reasons(analysis: TrackingAnalysis | None, settings: ReframeSettings) -> list[str]:
    """Why the stored analysis cannot be used for the selected mode (empty = it can).

    Results made for another mode are never used silently: a mode that needs data the
    analysis does not contain asks for a new analysis instead.
    """
    if settings.tracking_mode == "center":
        return []
    if analysis is None:
        return ["The video has not been analysed yet."]
    reasons = []
    missing = [f for f in sorted(settings.mode_info.requires) if not analysis.has(f)]
    if missing:
        reasons.append(f"{settings.mode_info.label} needs " + ", ".join(FEATURE_LABELS[f] for f in missing)
                       + ", which the current analysis does not contain.")
    if "mouth" in settings.mode_info.requires and analysis.has("mouth") and analysis.sample_fps < 10:
        reasons.append("Mouth movement was analysed at fewer than 10 frames per second, which is too few "
                       "to match it with speech.")
    if settings.confidence_threshold + 1e-6 < analysis.face_threshold:
        reasons.append(f"The detection threshold ({settings.confidence_threshold:.2f}) is lower than the one used "
                       f"for the analysis ({analysis.face_threshold:.2f}).")
    return reasons

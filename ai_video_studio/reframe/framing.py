"""Aspect-ratio presets, crop geometry and the smoothed crop path ("virtual camera").

The crop has a fixed size for the whole clip (so the output never zooms by itself) and
moves over time. Its position is computed from the tracking results in three steps:

1. **Desired position** for every keyframe: the followed subject placed at the chosen
   framing point, adjusted to keep the face out of the reserved caption area. While the
   subject is briefly missing the last position is held; after ``hold_seconds`` the crop
   returns to the centre.
2. **Zero-lag smoothing**: an exponential filter run forwards and then backwards over each
   continuous stretch, which removes detector jitter without the delay a live filter has.
   (Analysis is offline, so future frames are known.)
3. **Camera simulation**: a dead zone ignores small movements while the camera is at
   rest (once moving, it follows without a trailing offset), the speed is limited, and
   subject changes or reacquisitions become eased pans of ``transition_seconds``. The
   subject's face is always kept inside the crop, even if that exceeds the speed limit,
   and the crop is always kept inside the frame.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field

from ai_video_studio.reframe.model import ReframeError, ReframeSettings, Track, TrackingAnalysis, TrackSample

ASPECT_PRESETS: dict[str, tuple[int, int] | None] = {
    "original": None,
    "16:9": (16, 9),
    "9:16": (9, 16),
    "1:1": (1, 1),
    "4:5": (4, 5),
}
PRESET_LABELS = {
    "original": "Original aspect ratio",
    "16:9": "16:9 landscape (YouTube)",
    "9:16": "9:16 vertical (Reels, Shorts, TikTok)",
    "1:1": "1:1 square",
    "4:5": "4:5 portrait (feed)",
}
OUTPUT_SIZES: dict[str, list[tuple[int, int]]] = {
    "16:9": [(1920, 1080), (1280, 720), (3840, 2160)],
    "9:16": [(1080, 1920), (720, 1280)],
    "1:1": [(1080, 1080), (720, 720)],
    "4:5": [(1080, 1350), (864, 1080)],
}
PATH_RATE = 10.0  # crop keyframes per second (interpolated per output frame at export)
FACE_BONUS = 1.5  # automatic subject choice prefers faces over full-body detections


def even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def crop_size(src_w: int, src_h: int, preset: str, zoom: float = 1.0) -> tuple[int, int]:
    """Largest crop with the preset's aspect ratio that fits the frame, divided by ``zoom``."""
    if preset not in ASPECT_PRESETS:
        raise ReframeError(f"Unknown aspect preset '{preset}'")
    if src_w <= 0 or src_h <= 0:
        raise ReframeError("The video has no valid frame size.")
    ratio = ASPECT_PRESETS[preset]
    aspect = src_w / src_h if ratio is None else ratio[0] / ratio[1]
    zoom = max(1.0, zoom)
    if src_w / src_h > aspect:  # height limits the crop
        ch = min(even(src_h / zoom), even(src_h))
        cw = min(_nearest_even(ch * aspect), even(src_w))
    else:
        cw = min(even(src_w / zoom), even(src_w))
        ch = min(_nearest_even(cw / aspect), even(src_h))
    return cw, ch


def _nearest_even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def output_size_options(preset: str, src_w: int, src_h: int) -> list[tuple[int, int]]:
    """Output sizes offered for a preset; the first is the default."""
    if preset != "original":
        return list(OUTPUT_SIZES[preset])
    options = [(even(src_w), even(src_h))]
    long_side = max(src_w, src_h)
    for target in (1920, 1280):
        if long_side > target:
            k = target / long_side
            size = (even(src_w * k), even(src_h * k))
            if size not in options:
                options.append(size)
    return options


def resolve_output_size(settings: ReframeSettings, src_w: int, src_h: int) -> tuple[int, int]:
    if settings.output_width and settings.output_height:
        return even(settings.output_width), even(settings.output_height)
    return output_size_options(settings.preset, src_w, src_h)[0]


def anchor_region(kind: str, x: float, y: float, w: float, h: float) -> tuple[float, float, float, float]:
    """The part of a detection that framing protects: the face, or the top of a full body."""
    if kind == "person":
        return x, y, w, h * 0.4
    return x, y, w, h


def _smoothstep(p: float) -> float:
    p = max(0.0, min(1.0, p))
    return p * p * (3 - 2 * p)


@dataclass
class CropPath:
    """Crop rectangle over time. ``xs``/``ys`` are top-left corners in source pixels."""

    src_w: int
    src_h: int
    crop_w: int
    crop_h: int
    times: list[float]
    xs: list[float]
    ys: list[float]
    states: list[str] = field(default_factory=list)       # tracked | held | lost | center
    followed: list[int | None] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def _index(self, t: float) -> tuple[int, float]:
        i = bisect.bisect_right(self.times, t) - 1
        i = max(0, min(i, len(self.times) - 1))
        if i + 1 < len(self.times) and self.times[i + 1] > self.times[i]:
            k = (t - self.times[i]) / (self.times[i + 1] - self.times[i])
            return i, max(0.0, min(1.0, k))
        return i, 0.0

    def at(self, t: float) -> tuple[int, int]:
        """Top-left corner at time ``t`` (linear interpolation), always inside the frame."""
        i, k = self._index(t)
        x, y = self.xs[i], self.ys[i]
        if k > 0:
            x += (self.xs[i + 1] - x) * k
            y += (self.ys[i + 1] - y) * k
        x = min(max(0, round(x)), self.src_w - self.crop_w)
        y = min(max(0, round(y)), self.src_h - self.crop_h)
        return x, y

    def state_at(self, t: float) -> str:
        i, k = self._index(t)
        return self.states[i + (1 if k >= 0.5 and i + 1 < len(self.states) else 0)] if self.states else "center"

    def followed_at(self, t: float) -> int | None:
        i, k = self._index(t)
        if not self.followed:
            return None
        return self.followed[i + (1 if k >= 0.5 and i + 1 < len(self.followed) else 0)]

    @property
    def tracked_fraction(self) -> float:
        return sum(1 for s in self.states if s == "tracked") / len(self.states) if self.states else 0.0

    @property
    def is_static(self) -> bool:
        return max(self.xs) - min(self.xs) < 0.5 and max(self.ys) - min(self.ys) < 0.5

    def keyframes(self) -> list[list[float]]:
        return [[round(t, 3), round(x, 2), round(y, 2)] for t, x, y in zip(self.times, self.xs, self.ys)]

    @classmethod
    def from_keyframes(cls, src_w: int, src_h: int, crop_w: int, crop_h: int, keyframes: list) -> "CropPath":
        if not keyframes:
            raise ReframeError("The crop path is empty.")
        if not (0 < crop_w <= src_w and 0 < crop_h <= src_h):
            raise ReframeError("The crop does not fit inside the video frame.")
        rows = sorted((float(t), float(x), float(y)) for t, x, y in keyframes)
        return cls(src_w, src_h, crop_w, crop_h, [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows])


def auto_target_score(analysis: TrackingAnalysis, track: Track) -> float:
    bonus = FACE_BONUS if track.kind == "face" else 1.0
    return analysis.coverage(track) * track.mean_confidence * math.sqrt(track.mean_area) * bonus


def suitable_subjects(analysis: TrackingAnalysis, min_coverage: float = 0.05) -> list[Track]:
    """Tracks worth offering to the user, best first."""
    tracks = [t for t in analysis.tracks if analysis.coverage(t) >= min_coverage] or list(analysis.tracks)
    return sorted(tracks, key=lambda t: auto_target_score(analysis, t), reverse=True)


class _SubjectResolver:
    """Decides which track is followed at each time and returns its box."""

    REACQUIRE_SECONDS = 3.0      # how long after losing a subject a new track may continue it
    REACQUIRE_DISTANCE = 0.25    # max distance (fraction of frame width) between old and new track

    def __init__(self, analysis: TrackingAnalysis, settings: ReframeSettings):
        self.analysis = analysis
        self.settings = settings
        self.schedule = settings.subject_schedule()
        self.tolerance = analysis.sample_interval * 0.75
        self.max_gap = max(settings.hold_seconds, 2.5 * analysis.sample_interval)
        self.current: Track | None = None
        self.segment = -1
        self._continuations: dict[int, Track | None] = {}

    def _box(self, track: Track | None, t: float) -> TrackSample | None:
        return track.box_at(t, self.tolerance, self.max_gap) if track is not None else None

    def _best_visible(self, t: float) -> Track | None:
        visible = [tr for tr in self.analysis.tracks if self._box(tr, t) is not None]
        if not visible:
            return None
        return max(visible, key=lambda tr: auto_target_score(self.analysis, tr))

    def _continuation(self, lost: Track, t: float) -> Track | None:
        """The new track that continues ``lost`` (reappearance), once it has started.

        Candidates are tracks of the same kind that start within ``REACQUIRE_SECONDS``
        after ``lost`` ends, near its last position. With two or more candidates it is
        ambiguous who reappeared, so none is followed rather than risk jumping to a
        different person.
        """
        if lost.id not in self._continuations:
            last = lost.samples[-1]
            candidates = [
                tr for tr in self.analysis.tracks
                if tr is not lost and tr.kind == lost.kind and last.t < tr.start <= last.t + self.REACQUIRE_SECONDS
                and abs(tr.samples[0].cx - last.cx) <= self.REACQUIRE_DISTANCE
                and abs(tr.samples[0].cy - last.cy) <= self.REACQUIRE_DISTANCE
            ]
            self._continuations[lost.id] = candidates[0] if len(candidates) == 1 else None
        candidate = self._continuations[lost.id]
        return candidate if candidate is not None and t >= candidate.start else None

    def at(self, t: float) -> tuple[Track | None, TrackSample | None, bool]:
        """``(track, box, changed)``; ``changed`` is True when a different subject is now followed."""
        segment = max(i for i, (start, _tid) in enumerate(self.schedule) if start <= t + 1e-9)
        previous = self.current
        if segment != self.segment:
            self.segment = segment
            wanted = self.schedule[segment][1]
            if wanted is None:
                self.current = self._best_visible(t) or self.current
            else:
                self.current = self.analysis.track(wanted)
                if self.current is None:
                    raise ReframeError(f"Subject #{wanted} is not in the tracking results. Analyse the video again "
                                       f"or choose another subject.")
        box = self._box(self.current, t)
        if box is None and self.current is not None and t > self.current.end:
            automatic = self.schedule[self.segment][1] is None
            lost_for = t - self.current.end
            replacement = self._continuation(self.current, t)
            if replacement is None and automatic and lost_for > self.settings.hold_seconds:
                replacement = self._best_visible(t)
            if replacement is not None:
                self.current = replacement
                box = self._box(self.current, t)
        if box is None and self.current is None:
            self.current = self._best_visible(t) if self.schedule[self.segment][1] is None else None
            box = self._box(self.current, t)
        changed = previous is not None and self.current is not None and previous is not self.current
        return self.current, box, changed


def _ema(values: list[float], times: list[float], tau: float) -> list[float]:
    """Exponential smoothing run forwards and backwards (zero lag).

    The series is extended at both ends by odd reflection (as ``scipy.signal.filtfilt``
    does), so a subject moving steadily at the start or end of a stretch is not lagged
    or anticipated there.
    """
    n = len(values)
    if tau <= 0 or n < 3:
        return list(values)
    dt = max(1e-6, (times[-1] - times[0]) / (n - 1))
    alpha = 1 - math.exp(-dt / tau)
    pad = min(n - 1, int(math.ceil(3 * tau / dt)))
    first, last = values[0], values[-1]
    extended = ([2 * first - values[k] for k in range(pad, 0, -1)] + list(values)
                + [2 * last - values[n - 1 - k] for k in range(1, pad + 1)])

    def run(vals: list[float]) -> list[float]:
        out = [vals[0]]
        for v in vals[1:]:
            out.append(out[-1] + alpha * (v - out[-1]))
        return out

    smoothed = run(run(extended)[::-1])[::-1]
    return smoothed[pad:pad + n]


def compute_crop_path(
    analysis: TrackingAnalysis | None,
    settings: ReframeSettings,
    src_w: int,
    src_h: int,
    duration: float,
    rate: float = PATH_RATE,
) -> CropPath:
    settings.validate()
    cw, ch = crop_size(src_w, src_h, settings.preset, settings.zoom)
    max_x, max_y = src_w - cw, src_h - ch
    duration = max(0.0, duration)
    count = max(2, int(math.ceil(duration * rate)) + 1)
    times = [min(duration, i / rate) for i in range(count)]

    def clamp(x: float, y: float) -> tuple[float, float]:
        return min(max(0.0, x), max_x), min(max(0.0, y), max_y)

    center = clamp(max_x / 2 + settings.offset_x * cw, max_y / 2 + settings.offset_y * ch)
    if settings.mode == "center":
        return CropPath(src_w, src_h, cw, ch, times, [center[0]] * count, [center[1]] * count,
                        ["center"] * count, [None] * count)

    if analysis is None:
        raise ReframeError("Analyse the video first, or choose Center crop.")
    if not analysis.tracks:
        raise ReframeError("No faces or people were detected in this video, so there is nothing to follow. "
                           "Choose Center crop to export without tracking.")

    resolver = _SubjectResolver(analysis, settings)
    pad = settings.padding
    desired: list[tuple[float, float]] = []
    keep: list[tuple[float, float, float, float] | None] = []  # allowed range for x and y (lo_x, hi_x, lo_y, hi_y)
    states: list[str] = []
    followed: list[int | None] = []
    events: list[int] = []        # keyframe indices where a new continuous stretch starts
    caption_conflicts = 0
    last_desired = center
    last_seen: float | None = None

    for i, t in enumerate(times):
        track, box, changed = resolver.at(t)
        if box is not None:
            kx, ky, kw, kh = anchor_region(track.kind, box.x * src_w, box.y * src_h, box.w * src_w, box.h * src_h)
            ax = kx + kw / 2
            ay = ky + kh / 2
            dx = ax - cw / 2 + settings.offset_x * cw
            dy = ay - settings.subject_y * ch + settings.offset_y * ch
            px, py = kw * pad, kh * pad
            lo_x, hi_x = kx + kw + px - cw, kx - px
            lo_y, hi_y = ky + kh + py - ch, ky - py
            if settings.caption_safe != "off":
                usable = (1 - settings.caption_safe_height) * ch
                if settings.caption_safe == "bottom":
                    need_lo = ky + kh + py - usable       # face bottom above the caption area
                    if need_lo <= hi_y:
                        dy = max(dy, need_lo)
                        lo_y = max(lo_y, need_lo)
                    else:
                        caption_conflicts += 1
                else:
                    need_hi = ky - py - settings.caption_safe_height * ch  # face top below the caption area
                    if need_hi >= lo_y:
                        dy = min(dy, need_hi)
                        hi_y = min(hi_y, need_hi)
                    else:
                        caption_conflicts += 1
            state = "tracked"
            if lo_x > hi_x:  # subject wider than the crop: centre on it
                lo_x = hi_x = ax - cw / 2
            if lo_y > hi_y:
                lo_y = hi_y = ay - ch / 2
            target = clamp(dx, dy)
            if states and states[-1] == "lost" or changed:
                events.append(i)
            last_desired, last_seen = target, t
            keep.append((lo_x, hi_x, lo_y, hi_y))
        elif last_seen is not None and t - last_seen <= settings.hold_seconds:
            state, target = "held", last_desired
            keep.append(None)
        else:
            state, target = "lost", center
            if states and states[-1] != "lost":
                events.append(i)
            keep.append(None)
        desired.append(target)
        states.append(state)
        followed.append(track.id if track is not None and state != "lost" else None)

    # Zero-lag smoothing within each continuous stretch.
    tau = settings.smoothing * 1.2
    bounds = [0] + sorted(set(events)) + [count]
    smooth_x: list[float] = []
    smooth_y: list[float] = []
    for a, b in zip(bounds, bounds[1:]):
        if a == b:
            continue
        seg_t = times[a:b]
        smooth_x += _ema([d[0] for d in desired[a:b]], seg_t, tau)
        smooth_y += _ema([d[1] for d in desired[a:b]], seg_t, tau)

    # Camera simulation: dead zone, speed limit, eased transitions, keep the subject in frame.
    xs, ys = [smooth_x[0]], [smooth_y[0]]
    event_set = set(events)
    transition: tuple[int, float, float, float] | None = None  # (start index, start x, start y, start time)
    dz_x, dz_y = settings.dead_zone * cw, settings.dead_zone * ch
    panning = [False, False]
    for i in range(1, count):
        dt = max(1e-6, times[i] - times[i - 1])
        cx, cy = xs[-1], ys[-1]
        tx, ty = smooth_x[i], smooth_y[i]
        if i in event_set and settings.transition_seconds > 0:
            transition = (i, cx, cy, times[i - 1])
        if transition is not None:
            _start, sx, sy, t0 = transition
            p = _smoothstep((times[i] - t0) / settings.transition_seconds)
            cx, cy = sx + (tx - sx) * p, sy + (ty - sy) * p
            if p >= 1:
                transition = None
                panning = [False, False]
        else:
            step_x, step_y = settings.follow_speed * cw * dt, settings.follow_speed * ch * dt
            moved = []
            for axis, (c, tgt, dz, step) in enumerate(((cx, tx, dz_x, step_x), (cy, ty, dz_y, step_y))):
                diff = tgt - c
                # Dead zone with hysteresis: at rest, movements within the dead zone are ignored;
                # once the camera moves it follows fully (no constant trailing offset) and
                # comes to rest again when it has caught up.
                if not panning[axis] and abs(diff) > dz:
                    panning[axis] = True
                if panning[axis]:
                    c += max(-step, min(step, diff))
                    if abs(tgt - c) < 0.5:
                        panning[axis] = False
                moved.append(c)
            cx, cy = moved
            limits = keep[i]
            if limits is not None and states[i] == "tracked":
                lo_x, hi_x, lo_y, hi_y = limits
                cx = min(max(cx, lo_x), hi_x)
                cy = min(max(cy, lo_y), hi_y)
        cx, cy = clamp(cx, cy)
        xs.append(cx)
        ys.append(cy)

    path = CropPath(src_w, src_h, cw, ch, times, xs, ys, states, followed)
    tracked = path.tracked_fraction
    if tracked < 0.5:
        path.warnings.append(f"The subject was found in only {tracked:.0%} of the video. Elsewhere the framing is "
                             f"held or returns to the centre. Check the preview before exporting.")
    if caption_conflicts:
        path.warnings.append("At some moments the subject is too large to keep clear of the caption area.")
    return path

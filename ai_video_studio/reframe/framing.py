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
from dataclasses import dataclass, field, replace

from ai_video_studio.reframe.model import ReframeError, ReframeSettings, Track, TrackingAnalysis, TrackSample

ASPECT_PRESETS: dict[str, tuple[int, int] | None] = {
    "original": None,
    "16:9": (16, 9),
    "9:16": (9, 16),
    "1:1": (1, 1),
    "4:5": (4, 5),
}
CUSTOM_ASPECT = "custom"  # any output size (its own ratio); not listed with the presets above
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
FACE_BONUS = 1.3  # automatic subject choice prefers faces over full-body detections


def even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def crop_size(src_w: int, src_h: int, preset: str, zoom: float = 1.0,
              ratio: tuple[int, int] | None = None) -> tuple[int, int]:
    """Largest crop with the preset's aspect ratio that fits the frame, divided by ``zoom``.

    ``preset`` "custom" uses ``ratio`` (the output width and height), so any output size is
    framed without stretching the picture."""
    if preset == CUSTOM_ASPECT:
        if not ratio or ratio[0] <= 0 or ratio[1] <= 0:
            raise ReframeError("A custom aspect ratio needs an output width and height.")
    elif preset not in ASPECT_PRESETS:
        raise ReframeError(f"Unknown aspect preset '{preset}'")
    if src_w <= 0 or src_h <= 0:
        raise ReframeError("The video has no valid frame size.")
    ratio = ratio if preset == CUSTOM_ASPECT else ASPECT_PRESETS[preset]
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
    if preset == CUSTOM_ASPECT:
        raise ReframeError("A custom aspect ratio needs an output width and height.")
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
    """The part of a detection that framing protects: the face with chin and neck (the start of
    the upper body), or the top of a full body."""
    if kind == "person":
        return x, y, w, h * 0.4
    return x, y, w, h * 1.35


def _smoothstep(p: float) -> float:
    p = max(0.0, min(1.0, p))
    return p * p * (3 - 2 * p)


@dataclass
class CropPath:
    """Crop rectangle over time. ``xs``/``ys`` are top-left corners in source pixels.

    Preview and export both use this object (the export payload stores its keyframes), so
    they always show the same framing.
    """

    src_w: int
    src_h: int
    crop_w: int
    crop_h: int
    times: list[float]
    xs: list[float]
    ys: list[float]
    states: list[str] = field(default_factory=list)       # tracked | held | lost | silence | center
    followed: list[int | None] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    speech: list[float | None] = field(default_factory=list)          # speech share around each keyframe
    speaker_score: list[float | None] = field(default_factory=list)   # evidence of the followed speaker
    upscale: float = 1.0
    zoom_used: float = 1.0

    def _index(self, t: float) -> tuple[int, float]:
        i = bisect.bisect_right(self.times, t) - 1
        i = max(0, min(i, len(self.times) - 1))
        if i + 1 < len(self.times) and self.times[i + 1] > self.times[i]:
            k = (t - self.times[i]) / (self.times[i + 1] - self.times[i])
            return i, max(0.0, min(1.0, k))
        return i, 0.0

    def _nearest(self, values: list, t: float, default=None):
        if not values:
            return default
        i, k = self._index(t)
        return values[i + (1 if k >= 0.5 and i + 1 < len(values) else 0)]

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
        return self._nearest(self.states, t, "center")

    def followed_at(self, t: float) -> int | None:
        return self._nearest(self.followed, t)

    def speech_at(self, t: float) -> float | None:
        return self._nearest(self.speech, t)

    def speaker_score_at(self, t: float) -> float | None:
        return self._nearest(self.speaker_score, t)

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
    """How suitable a track is as the main subject: time on screen x detection reliability x
    confidence, with faces preferred. Size only breaks ties, so a large face of someone passing
    close to the camera does not win over the person the video is about."""
    bonus = FACE_BONUS if track.kind == "face" else 1.0
    presence = analysis.screen_share(track) * max(analysis.duration, 1e-6)
    return presence * analysis.detection_rate(track) * track.mean_confidence * bonus + 1e-3 * math.sqrt(track.mean_area)


def suitable_subjects(analysis: TrackingAnalysis) -> list[Track]:
    """Tracks to offer to the user, best first."""
    return sorted(analysis.tracks, key=lambda t: auto_target_score(analysis, t), reverse=True)


def caption_band_from_style(style) -> tuple[str, float] | None:
    """``(position, height fraction)`` of the output covered by captions in ``style``
    (a ``CaptionStyle``). Caption sizes scale with the output height, so the fraction is the
    same for every output size. ``None`` for captions in the middle (no band to keep clear)."""
    if style.vertical not in ("bottom", "top"):
        return None
    lines_px = style.max_lines * style.font_size * 1.2 + 2 * style.outline_width
    if style.background:
        lines_px += 2 * style.background_padding
    fraction = style.margin_v / 100 + lines_px / 1080 + 0.02
    return style.vertical, min(0.5, max(0.05, fraction))


def filter_by_confidence(analysis: TrackingAnalysis, threshold: float) -> TrackingAnalysis:
    """A view of ``analysis`` without detections below ``threshold`` (tracks left with fewer
    than two detections are dropped)."""
    if threshold <= analysis.face_threshold + 1e-9:
        return analysis
    tracks = []
    for track in analysis.tracks:
        samples = [s for s in track.samples if s.confidence >= threshold]
        if len(samples) >= 2:
            tracks.append(Track(track.id, track.kind, samples, track.appearance))
    return replace(analysis, tracks=tracks)


@dataclass
class _Decision:
    track: Track | None
    box: TrackSample | None
    state: str                 # tracked | held | lost | silence
    changed: bool = False      # a different subject is followed from here
    speech: float | None = None
    score: float | None = None


class _Resolver:
    """Decides, for each keyframe, whom to follow according to the tracking mode."""

    def __init__(self, analysis: TrackingAnalysis, settings: ReframeSettings):
        self.analysis = analysis
        self.settings = settings
        self.mode = settings.tracking_mode
        self.tolerance = analysis.sample_interval * 0.75
        self.max_gap = max(settings.hold_seconds, 2.5 * analysis.sample_interval)
        if self.mode in ("auto_face", "active_speaker"):
            self.kinds = {"face"}
        elif self.mode == "speech_audio":
            self.kinds = {"face"} if any(t.kind == "face" for t in analysis.tracks) else {"person"}
        else:
            self.kinds = {"face", "person"}
        self.candidates = [t for t in analysis.tracks if t.kind in self.kinds]
        self._rank = {t.id: auto_target_score(analysis, t) for t in self.candidates}

    def box(self, track: Track | None, t: float) -> TrackSample | None:
        return track.box_at(t, self.tolerance, self.max_gap) if track is not None else None

    def visible(self, t: float) -> list[tuple[Track, TrackSample]]:
        out = []
        for track in self.candidates:
            b = self.box(track, t)
            if b is not None:
                out.append((track, b))
        return out

    def best(self, tracks) -> Track | None:
        tracks = list(tracks)
        return max(tracks, key=lambda tr: self._rank[tr.id]) if tracks else None

    # -- subject modes: manual / auto_face / auto_visible -----------------------------------------
    def _wanted(self, track_id: int | None) -> Track | None:
        if track_id is None:
            if self.mode == "manual":
                raise ReframeError("Manual Subject Tracking needs a subject: select one and click "
                                   "Follow From Start.")
            return None
        track = self.analysis.track(track_id)
        if track is None:
            raise ReframeError(f"Subject #{track_id} is not in the tracking results. Analyse the video again "
                               f"or choose another subject.")
        if track.kind not in self.kinds:
            raise ReframeError(f"Subject #{track_id} is a full-body detection; Auto Face Tracking follows faces. "
                               f"Choose a face, or use Manual Subject Tracking.")
        return track

    def subjects(self, times: list[float]) -> list[_Decision]:
        schedule = self.settings.subject_schedule()
        decisions = []
        current: Track | None = None
        automatic = False
        segment = -1
        last_seen: float | None = None
        for t in times:
            changed = False
            seg = max(i for i, (start, _tid) in enumerate(schedule) if start <= t + 1e-9)
            if seg != segment:
                segment = seg
                wanted = self._wanted(schedule[seg][1])
                automatic = wanted is None
                previous = current
                if automatic:
                    if self.mode == "auto_face":
                        # Start with the best face on screen (by screen time and reliability, not
                        # size) and stay with it; only if none is visible, wait for the best one.
                        pool = [tr for tr in self.candidates if tr.end >= t] or self.candidates
                        wanted = self.best(tr for tr, _b in self.visible(t)) or self.best(pool)
                    else:
                        wanted = self.best(tr for tr, _b in self.visible(t)) or current
                current = wanted
                changed = previous is not None and current is not previous
            box = self.box(current, t)
            if box is None and current is not None and automatic:
                lost_for = t - (last_seen if last_seen is not None else t)
                if lost_for > self.settings.hold_seconds or last_seen is None:
                    replacement = None
                    if self.mode == "auto_visible":
                        replacement = self.best(tr for tr, _b in self.visible(t) if tr is not current)
                    elif self.mode == "auto_face" and t > current.end:
                        # The main face has left for good (re-linking found no continuation).
                        replacement = self.best(tr for tr, _b in self.visible(t) if tr is not current)
                    if replacement is not None:
                        current, changed = replacement, True
                        box = self.box(current, t)
            if box is not None:
                last_seen = t
                decisions.append(_Decision(current, box, "tracked", changed))
            elif last_seen is not None and t - last_seen <= self.settings.hold_seconds:
                decisions.append(_Decision(current, None, "held", changed))
            else:
                decisions.append(_Decision(current, None, "lost", changed))
        return decisions

    # -- active speaker ---------------------------------------------------------------------------
    def active_speaker(self, times: list[float]) -> list[_Decision]:
        from ai_video_studio.reframe.speaker import SpeakerEvidence

        evidence = SpeakerEvidence(self.analysis, self.settings.speech_threshold)
        decisions = []
        current: Track | None = None
        pending: int | None = None
        pending_since = 0.0
        confirmed_at: float | None = None
        last_seen: float | None = None
        for t in times:
            visible = self.visible(t)
            moment = evidence.moment(t, [tr for tr, _b in visible])
            changed = False
            if moment.state == "speaker":
                if current is not None and moment.track_id == current.id:
                    pending, confirmed_at = None, t
                else:
                    if pending != moment.track_id:
                        pending, pending_since = moment.track_id, t
                    need = self.settings.switch_seconds if current is not None else self.settings.switch_seconds / 2
                    if t - pending_since >= need - 1e-9:
                        previous = current
                        current = self.analysis.track(pending)
                        changed = previous is not None
                        pending, confirmed_at = None, t
            else:
                pending = None
                if moment.state == "silence" and current is not None:
                    confirmed_at = t  # no switching during silence: the last speaker stays
                elif (moment.state == "uncertain" and current is not None and confirmed_at is not None
                      and t - confirmed_at > self.settings.speaker_hold_seconds):
                    current = None  # nobody visible can be matched to the speech for too long
            box = self.box(current, t)
            score = moment.evidence.get(current.id) if current is not None else None
            if box is not None:
                last_seen = t
                decisions.append(_Decision(current, box, "tracked", changed, moment.speech, score))
            elif current is not None and last_seen is not None and t - last_seen <= self.settings.hold_seconds:
                decisions.append(_Decision(current, None, "held", changed, moment.speech, score))
            else:
                decisions.append(_Decision(None, None, "lost", changed, moment.speech, None))
        return decisions

    # -- speech-audio-driven ----------------------------------------------------------------------
    def speech_driven(self, times: list[float]) -> list[_Decision]:
        speech = self.analysis.speech
        decisions = []
        current: Track | None = None
        for t in times:
            share = speech.speech_fraction(t - 0.25, t + 0.25, self.settings.speech_threshold)
            if share < 0.5:
                # Silence: hold the framing still (no change of subject without speech).
                decisions.append(_Decision(current, None, "silence", False, share))
                continue
            visible = self.visible(t)
            previous = current
            if len(visible) == 1:
                current = visible[0][0]
            elif len(visible) > 1:
                # Audio cannot tell which visible person speaks: keep the previous target (if the
                # fallback is "hold" and they are still visible) rather than guess.
                still = [tr for tr, _b in visible if tr is current]
                current = still[0] if still and self.settings.fallback == "hold" else None
            else:
                current = None
            box = self.box(current, t)
            changed = previous is not None and current is not None and current is not previous
            decisions.append(_Decision(current, box, "tracked" if box is not None else "lost", changed, share))
        return decisions

    def resolve(self, times: list[float]) -> list[_Decision]:
        if self.mode == "active_speaker":
            return self.active_speaker(times)
        if self.mode == "speech_audio":
            return self.speech_driven(times)
        return self.subjects(times)


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


def effective_crop(settings: ReframeSettings, src_w: int, src_h: int) -> tuple[int, int, float, list[str], list[str]]:
    """Crop size after applying the maximum upscale. Returns (w, h, zoom used, warnings, notes)."""
    out_w, _out_h = resolve_output_size(settings, src_w, src_h)
    ratio = (settings.output_width, settings.output_height) if settings.preset == CUSTOM_ASPECT else None
    full_w, _full_h = crop_size(src_w, src_h, settings.preset, 1.0, ratio)
    warnings, notes = [], []
    full_upscale = out_w / full_w
    max_zoom = max(1.0, settings.max_upscale * full_w / out_w)
    zoom = min(settings.zoom, max_zoom)
    if zoom < settings.zoom - 1e-6:
        notes.append(f"Zoom limited to {zoom:.2f}× so the crop is not enlarged more than "
                     f"{settings.max_upscale:.2f}× (maximum upscale).")
    if full_upscale > settings.max_upscale + 1e-6:
        better = [s for s in output_size_options(settings.preset, src_w, src_h)
                  if s[0] / full_w <= settings.max_upscale + 1e-6]
        hint = (f" Choose {better[0][0]}×{better[0][1]} ({better[0][0] / full_w:.2f}×)" if better
                else " No preset size stays within the limit")
        warnings.append(f"The source is too small for this output: the {full_w}×{_full_h} crop is enlarged "
                        f"{full_upscale:.2f}×, above the {settings.max_upscale:.2f}× limit, so the result will be "
                        f"soft.{hint}, or raise the limit.")
    cw, ch = crop_size(src_w, src_h, settings.preset, zoom, ratio)
    return cw, ch, zoom, warnings, notes


def compute_crop_path(
    analysis: TrackingAnalysis | None,
    settings: ReframeSettings,
    src_w: int,
    src_h: int,
    duration: float,
    rate: float = PATH_RATE,
    caption_band: tuple[str, float] | None = None,
) -> CropPath:
    """The crop over time for ``settings.tracking_mode``.

    ``caption_band`` is the caption area of the output as ``(position, fraction)``, used when
    ``settings.caption_safe`` is "auto" (derived from the caption style by the caller).
    """
    from ai_video_studio.reframe.model import reanalysis_reasons

    settings.validate()
    cw, ch, zoom_used, warnings, notes = effective_crop(settings, src_w, src_h)
    out_w, _out_h = resolve_output_size(settings, src_w, src_h)
    max_x, max_y = src_w - cw, src_h - ch
    duration = max(0.0, duration)
    count = max(2, int(math.ceil(duration * rate)) + 1)
    times = [min(duration, i / rate) for i in range(count)]

    def clamp(x: float, y: float) -> tuple[float, float]:
        return min(max(0.0, x), max_x), min(max(0.0, y), max_y)

    def finish(path: CropPath) -> CropPath:
        path.warnings = warnings + path.warnings
        path.notes = notes + path.notes
        path.upscale, path.zoom_used = out_w / cw, zoom_used
        return path

    center = clamp(max_x / 2 + settings.offset_x * cw, max_y / 2 + settings.offset_y * ch)
    if settings.tracking_mode == "center":
        return finish(CropPath(src_w, src_h, cw, ch, times, [center[0]] * count, [center[1]] * count,
                               ["center"] * count, [None] * count))

    if analysis is None:
        raise ReframeError("Analyse the video first, or choose Center crop.")
    reasons = reanalysis_reasons(analysis, settings)
    if reasons:
        raise ReframeError("Reanalysis required: " + " ".join(reasons))
    analysis = filter_by_confidence(analysis, settings.confidence_threshold)
    if not analysis.tracks and settings.tracking_mode != "speech_audio":
        raise ReframeError("No faces or people were detected in this video, so there is nothing to follow. "
                           "Choose Center crop to export without tracking.")

    band = None
    if settings.caption_safe == "auto":
        band = caption_band if settings.burn_captions else None
    elif settings.caption_safe in ("bottom", "top"):
        band = (settings.caption_safe, settings.caption_safe_height)

    resolver = _Resolver(analysis, settings)
    decisions = resolver.resolve(times)
    pad = settings.padding
    desired: list[tuple[float, float]] = []
    keep: list[tuple[float, float, float, float] | None] = []  # allowed range for x and y (lo_x, hi_x, lo_y, hi_y)
    protected: list[tuple[float, float, float, float] | None] = []
    states: list[str] = []
    events: list[int] = []        # keyframe indices where a new continuous stretch starts
    last_desired: tuple[float, float] | None = None

    def fallback_position(t: float) -> tuple[float, float]:
        if settings.fallback == "hold" and last_desired is not None:
            return last_desired
        if settings.fallback == "group":
            boxes = [b for _tr, b in resolver.visible(t)]
            if boxes:
                x0 = min(b.x for b in boxes) * src_w
                x1 = max(b.x + b.w for b in boxes) * src_w
                y0 = min(b.y for b in boxes) * src_h
                y1 = max(b.y + b.h for b in boxes) * src_h
                return clamp((x0 + x1) / 2 - cw / 2 + settings.offset_x * cw,
                             y0 - settings.subject_y * ch + (y1 - y0) / 2 + settings.offset_y * ch)
            if last_desired is not None:
                return last_desired
        return center

    for i, (t, d) in enumerate(zip(times, decisions)):
        box = d.box
        if d.state == "tracked" and box is not None:
            kind = d.track.kind
            kx, ky, kw, kh = anchor_region(kind, box.x * src_w, box.y * src_h, box.w * src_w, box.h * src_h)
            if kind == "person":
                ax, ay = kx + kw / 2, ky + kh / 2
            else:
                ax, ay = (box.x + box.w / 2) * src_w, (box.y + box.h / 2) * src_h
            dx = ax - cw / 2 + settings.offset_x * cw
            dy = ay - settings.subject_y * ch + settings.offset_y * ch
            px, py = kw * pad, kh * pad
            lo_x, hi_x = kx + kw + px - cw, kx - px
            lo_y, hi_y = ky + kh + py - ch, ky - py
            if lo_x > hi_x:  # subject wider than the crop: centre on it
                lo_x = hi_x = ax - cw / 2
            if lo_y > hi_y:
                lo_y = hi_y = ay - ch / 2
            if band is not None:
                # Soft preference (applied before smoothing, so it never makes the camera jump):
                # move the crop so the face stays out of the caption band, within what keeps the
                # face itself in frame.
                usable = (1 - band[1]) * ch
                if band[0] == "bottom":
                    dy = max(dy, min(ky + kh + py - usable, hi_y))
                else:
                    dy = min(dy, max(ky - py - band[1] * ch, lo_y))
            target = clamp(dx, dy)
            if (states and states[-1] in ("lost", "silence")) or d.changed:
                events.append(i)
            last_desired = target
            keep.append((lo_x, hi_x, lo_y, hi_y))
            protected.append((kx, ky, kw, kh))
            state = "tracked"
        elif d.state == "held" or d.state == "silence":
            target = last_desired if last_desired is not None else center
            keep.append(None)
            protected.append(None)
            state = d.state
        else:
            target = fallback_position(t)
            if states and states[-1] not in ("lost",):
                events.append(i)
            keep.append(None)
            protected.append(None)
            state = "lost"
        desired.append(target)
        states.append(state)

    # Zero-lag smoothing within each continuous stretch.
    tau = settings.smoothing * 1.2
    bounds = [0] + sorted(set(events)) + [count]
    smooth_x: list[float] = []
    smooth_y: list[float] = []
    for a, b in zip(bounds, bounds[1:]):
        if a == b:
            continue
        seg_t = times[a:b]
        smooth_x += _ema([p[0] for p in desired[a:b]], seg_t, tau)
        smooth_y += _ema([p[1] for p in desired[a:b]], seg_t, tau)

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
        elif states[i] == "silence":
            pass  # speech-driven mode: the framing holds still while nobody speaks
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

    path = CropPath(src_w, src_h, cw, ch, times, xs, ys, states, [d.track.id if d.track is not None and s != "lost"
                                                                  else None for d, s in zip(decisions, states)],
                    speech=[d.speech for d in decisions], speaker_score=[d.score for d in decisions])
    tracked = path.tracked_fraction
    if settings.tracking_mode in ("manual", "auto_face", "auto_visible") and tracked < 0.5:
        path.warnings.append(f"The subject is in frame for only {tracked:.0%} of the video. Elsewhere the framing is "
                             f"held or uses the fallback. Check the preview before exporting.")
    if settings.tracking_mode == "active_speaker":
        named = sum(1 for d in decisions if d.state == "tracked") / count
        if named < 0.2:
            path.warnings.append("A visible speaker could be identified for only "
                                 f"{named:.0%} of the video (off-screen voice, no clear mouth movement, or "
                                 f"overlapping speech). The rest uses the fallback.")
    if settings.tracking_mode == "speech_audio" and not any(t.kind == "face" for t in analysis.tracks):
        path.notes.append("No faces were found, so speech timing can only hold or move the fallback framing.")
    if band is not None:
        overlap = 0
        tracked_frames = 0
        for x, y, region, state in zip(xs, ys, protected, states):
            if region is None or state != "tracked":
                continue
            tracked_frames += 1
            _kx, ky, _kw, kh = region
            top, bottom = y + (1 - band[1]) * ch, y + band[1] * ch
            if (band[0] == "bottom" and ky + kh > top + 1) or (band[0] == "top" and ky < bottom - 1):
                overlap += 1
        if tracked_frames and overlap / tracked_frames > 0.05:
            reason = (" The crop uses the full frame height, so it cannot move vertically."
                      if ch >= src_h - 1 else "")
            path.warnings.append(f"The face overlaps the caption area in {overlap / tracked_frames:.0%} of the tracked "
                                 f"time.{reason} Move the captions to the other edge (Captions → Style) or zoom in "
                                 f"slightly.")
    return finish(path)

"""Multi-subject tracker for detections on sampled frames.

Online association (frame by frame): detections are matched to existing tracks of the same
kind by overlap (IoU) with the box predicted from each track's recent motion, falling back to
centre distance for small or fast-moving boxes. A match is refused when the size changes too
much between frames (a large foreground face is not the small face behind it) or when the
colour appearance clearly differs. Matching is greedy, best pair first, so two people next to
each other keep their own IDs. Tracks seen in fewer than ``min_hits`` frames are discarded as
likely false detections.

Offline re-linking (after the whole video): a person hidden for longer than
``max_missed_seconds`` (someone walking in front, turning away) first gets a new track. Track
pieces are then joined when one starts shortly after another ends, near where it was last seen
(allowing for movement), at a similar size and with a similar appearance, and only when that
choice is unambiguous. This keeps one ID per person through occlusions without guessing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ai_video_studio.reframe.model import Detection, Track, TrackSample

MAX_SIZE_RATIO = 1.6        # largest allowed change of box size (linear) between matches
MIN_APPEARANCE = 0.35       # below this similarity a match is refused (when both have appearance)
RELINK_APPEARANCE = 0.5     # stricter similarity for joining pieces after a gap
RELINK_SECONDS = 6.0        # longest gap bridged by re-linking
RELINK_SPEED = 1.5          # assumed movement while hidden, in box sizes per second
RELINK_AMBIGUITY = 0.1      # two candidates closer than this in score: do not re-link


def iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def similarity(a, b) -> float | None:
    """Histogram intersection of two normalised histograms (1 = identical), or None if unknown."""
    if a is None or b is None or len(a) != len(b):
        return None
    return sum(min(x, y) for x, y in zip(a, b))


def size_ratio(w1: float, h1: float, w2: float, h2: float) -> float:
    a, b = math.sqrt(max(w1 * h1, 1e-12)), math.sqrt(max(w2 * h2, 1e-12))
    return max(a, b) / min(a, b)


@dataclass
class _ActiveTrack:
    key: int
    kind: str
    samples: list[TrackSample] = field(default_factory=list)
    vx: float = 0.0  # centre velocity, normalised units per second
    vy: float = 0.0
    appearance: list[float] | None = None
    appearance_count: int = 0

    @property
    def last(self) -> TrackSample:
        return self.samples[-1]

    def predicted_box(self, t: float) -> tuple[float, float, float, float]:
        s = self.last
        dt = max(0.0, t - s.t)
        return s.x + self.vx * dt, s.y + self.vy * dt, s.w, s.h

    def add(self, t: float, det: Detection) -> None:
        if self.samples:
            prev = self.last
            dt = t - prev.t
            if dt > 0:
                vx, vy = (det.cx - prev.cx) / dt, (det.cy - prev.cy) / dt
                # Smoothed velocity: one noisy detection should not throw the prediction off.
                self.vx, self.vy = 0.5 * self.vx + 0.5 * vx, 0.5 * self.vy + 0.5 * vy
        self.samples.append(TrackSample(t, det.x, det.y, det.w, det.h, det.confidence))
        if det.appearance is not None:
            n = self.appearance_count
            if self.appearance is None:
                self.appearance = list(det.appearance)
            else:  # running mean
                self.appearance = [(a * n + b) / (n + 1) for a, b in zip(self.appearance, det.appearance)]
            self.appearance_count = n + 1


class IouTracker:
    def __init__(self, aspect: float = 16 / 9, iou_threshold: float = 0.2, max_distance: float = 2.5,
                 max_missed_seconds: float = 1.5, min_hits: int = 2):
        """``aspect`` = frame width / height, so centre distances are measured in real proportions.

        ``max_distance`` is the largest centre distance for a match, in multiples of the
        track's box size (the larger of its width and height).
        """
        self.aspect = aspect
        self.iou_threshold = iou_threshold
        self.max_distance = max_distance
        self.max_missed_seconds = max_missed_seconds
        self.min_hits = min_hits
        self._active: list[_ActiveTrack] = []
        self._closed: list[_ActiveTrack] = []
        self._next_key = 1

    def _distance(self, box: tuple[float, float, float, float], cx: float, cy: float) -> float:
        """Centre distance in multiples of the box size (box size measured in frame heights)."""
        x, y, w, h = box
        dx = (x + w / 2 - cx) * self.aspect
        dy = y + h / 2 - cy
        size = max(w * self.aspect, h, 1e-6)
        return math.hypot(dx, dy) / size

    def update(self, t: float, detections: list[Detection]) -> list[tuple[int, Detection]]:
        """Add one analysed frame. Returns ``(internal_key, detection)`` for every detection."""
        for track in [tr for tr in self._active if t - tr.last.t > self.max_missed_seconds]:
            self._active.remove(track)
            self._closed.append(track)

        pairs = []
        for ti, track in enumerate(self._active):
            box = track.predicted_box(t)
            for di, det in enumerate(detections):
                if det.kind != track.kind:
                    continue
                if size_ratio(box[2], box[3], det.w, det.h) > MAX_SIZE_RATIO:
                    continue  # a much bigger or smaller box is a different person
                sim = similarity(track.appearance, det.appearance)
                if sim is not None and sim < MIN_APPEARANCE:
                    continue  # clearly different colours (face, hair, clothes)
                overlap = iou(box, (det.x, det.y, det.w, det.h))
                distance = self._distance(box, det.cx, det.cy)
                if overlap >= self.iou_threshold or distance <= self.max_distance:
                    # Prefer overlap; distance and appearance break ties.
                    bonus = 0.0 if sim is None else 0.3 * (sim - 0.5)
                    pairs.append((overlap - 0.1 * distance + bonus, ti, di))
        pairs.sort(reverse=True)
        used_tracks: set[int] = set()
        matched: dict[int, _ActiveTrack] = {}
        for _score, ti, di in pairs:
            if ti in used_tracks or di in matched:
                continue
            used_tracks.add(ti)
            matched[di] = self._active[ti]

        results = []
        for di, det in enumerate(detections):
            track = matched.get(di)
            if track is None:
                track = _ActiveTrack(self._next_key, det.kind)
                self._next_key += 1
                self._active.append(track)
            track.add(t, det)
            results.append((track.key, det))
        return results

    def annotate_mouth(self, key: int, value: float | None) -> None:
        """Attach mouth movement to the latest sample of track ``key``."""
        for track in self._active:
            if track.key == key and track.samples:
                s = track.last
                track.samples[-1] = TrackSample(s.t, s.x, s.y, s.w, s.h, s.confidence, value)
                return

    def finish(self) -> list[Track]:
        """Close all tracks, re-link pieces of the same subject and return the confirmed tracks,
        numbered 1, 2, ... by first appearance."""
        tracks = [t for t in self._closed + self._active if len(t.samples) >= self.min_hits]
        self._closed, self._active = [], []
        chains = relink([Track(id=t.key, kind=t.kind, samples=list(t.samples), appearance=t.appearance)
                         for t in tracks], self.aspect)
        chains.sort(key=lambda t: (t.samples[0].t, t.id))
        return [Track(id=i, kind=t.kind, samples=t.samples, appearance=t.appearance)
                for i, t in enumerate(chains, start=1)]


def _relink_score(a: Track, b: Track, aspect: float) -> float | None:
    """How plausible it is that ``b`` continues ``a`` after a gap (None = impossible)."""
    if a.kind != b.kind:
        return None
    gap = b.start - a.end
    if not 0 < gap <= RELINK_SECONDS:
        return None
    last, first = a.samples[-1], b.samples[0]
    if size_ratio(last.w, last.h, first.w, first.h) > MAX_SIZE_RATIO:
        return None
    size = max(last.w * aspect, last.h, 1e-6)
    distance = math.hypot((first.cx - last.cx) * aspect, first.cy - last.cy) / size
    allowed = 1.5 + RELINK_SPEED * gap
    if distance > allowed:
        return None
    sim = similarity(a.appearance, b.appearance)
    if sim is not None and sim < RELINK_APPEARANCE:
        return None
    appearance_term = 0.5 if sim is None else sim
    return appearance_term - 0.3 * distance / allowed - 0.05 * gap


def relink(tracks: list[Track], aspect: float) -> list[Track]:
    """Join track pieces that are the same subject (see module docstring)."""
    candidates: dict[int, list[tuple[float, int]]] = {}   # predecessor -> [(score, successor)]
    predecessors: dict[int, list[tuple[float, int]]] = {}
    by_id = {t.id: t for t in tracks}
    for a in tracks:
        for b in tracks:
            if a is b:
                continue
            score = _relink_score(a, b, aspect)
            if score is not None:
                candidates.setdefault(a.id, []).append((score, b.id))
                predecessors.setdefault(b.id, []).append((score, a.id))

    def unambiguous(options: list[tuple[float, int]]) -> int | None:
        options = sorted(options, reverse=True)
        if len(options) > 1 and options[0][0] - options[1][0] < RELINK_AMBIGUITY:
            return None
        return options[0][1]

    successor: dict[int, int] = {}
    for a_id, options in candidates.items():
        b_id = unambiguous(options)
        if b_id is None or unambiguous(predecessors[b_id]) != a_id:
            continue
        successor[a_id] = b_id
    has_predecessor = set(successor.values())
    chains = []
    for track in sorted(tracks, key=lambda t: t.start):
        if track.id in has_predecessor:
            continue
        samples = list(track.samples)
        appearance, weight = track.appearance, len(track.samples)
        current = track.id
        while current in successor:
            nxt = by_id[successor[current]]
            if nxt.start <= samples[-1].t:
                break  # never overlap in time
            samples += nxt.samples
            if nxt.appearance is not None:
                if appearance is None:
                    appearance, weight = list(nxt.appearance), len(nxt.samples)
                else:
                    n = len(nxt.samples)
                    appearance = [(x * weight + y * n) / (weight + n) for x, y in zip(appearance, nxt.appearance)]
                    weight += n
            current = nxt.id
        chains.append(Track(id=track.id, kind=track.kind, samples=samples, appearance=appearance))
    return chains

"""Multi-subject tracker for detections on sampled frames.

Detections are matched to existing tracks of the same kind by overlap (IoU) of the box
predicted from each track's recent motion, falling back to centre distance for small or
fast-moving boxes. Matching is greedy, best pair first, so two people next to each other
keep their own IDs. A track survives short gaps (occlusion, a missed detection) and is
closed after ``max_missed_seconds`` without a match. Tracks seen in fewer than
``min_hits`` frames are discarded as likely false detections.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ai_video_studio.reframe.model import Detection, Track, TrackSample


def iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


@dataclass
class _ActiveTrack:
    key: int
    kind: str
    samples: list[TrackSample] = field(default_factory=list)
    vx: float = 0.0  # centre velocity, normalised units per second
    vy: float = 0.0

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

    def _distance(self, box: tuple[float, float, float, float], det: Detection) -> float:
        """Centre distance in multiples of the box size (box size measured in frame heights)."""
        x, y, w, h = box
        dx = (x + w / 2 - det.cx) * self.aspect
        dy = y + h / 2 - det.cy
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
                overlap = iou(box, (det.x, det.y, det.w, det.h))
                distance = self._distance(box, det)
                if overlap >= self.iou_threshold or distance <= self.max_distance:
                    # Prefer overlap; distance only breaks ties and handles non-overlapping moves.
                    pairs.append((overlap - 0.1 * distance, ti, di))
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

    def finish(self) -> list[Track]:
        """Close all tracks and return the confirmed ones, numbered 1, 2, ... by first appearance."""
        tracks = self._closed + self._active
        self._closed, self._active = [], []
        confirmed = [t for t in tracks if len(t.samples) >= self.min_hits]
        confirmed.sort(key=lambda t: (t.samples[0].t, t.key))
        return [Track(id=i, kind=t.kind, samples=list(t.samples)) for i, t in enumerate(confirmed, start=1)]

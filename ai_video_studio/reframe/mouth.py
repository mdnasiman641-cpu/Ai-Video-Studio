"""Mouth-region movement per face, used as visual evidence of speaking.

YuNet gives five landmarks per face, including both mouth corners but no lip contour, so
mouth opening cannot be measured directly. Instead the mouth region (between and below the
mouth corners) is cut out in consecutive analysed frames, normalised for brightness and
contrast, and compared: talking changes this region frame to frame. Movement of the whole
head or lighting changes also change it, so the same measure is taken on the eye region and
subtracted. The result is a non-negative number per face per frame; ``None`` when there is no
previous frame of the same face to compare with.

This is a hand-built measure, not a trained lip-reading or active-speaker model. Chewing,
smiling or laughing also move the mouth; the active-speaker logic therefore only uses it
together with the speech in the audio (see ``reframe.speaker``).
"""

from __future__ import annotations

import math

from ai_video_studio.reframe.model import Detection

PATCH_SIZE = (24, 12)  # width, height of the normalised region


def mouth_regions(det: Detection, width: int, height: int):
    """``(mouth_box, reference_box)`` in pixels as (x0, y0, x1, y1), or None without landmarks."""
    if det.landmarks is None or len(det.landmarks) < 10:
        return None
    p = [v * (width if i % 2 == 0 else height) for i, v in enumerate(det.landmarks)]
    (rex, rey), (lex, ley), (rmx, rmy), (lmx, lmy) = p[0:2], p[2:4], p[6:8], p[8:10]  # p[4:6] = nose
    mouth_w = max(math.hypot(lmx - rmx, lmy - rmy), det.w * width * 0.25)
    cx, cy = (rmx + lmx) / 2, (rmy + lmy) / 2 + 0.1 * mouth_w
    mouth = (cx - 0.75 * mouth_w, cy - 0.45 * mouth_w, cx + 0.75 * mouth_w, cy + 0.45 * mouth_w)
    ex, ey = (rex + lex) / 2, (rey + ley) / 2
    ref = (ex - 0.75 * mouth_w, ey - 0.45 * mouth_w, ex + 0.75 * mouth_w, ey + 0.45 * mouth_w)
    return mouth, ref


def _patch(gray, box):
    import numpy as np

    from ai_video_studio.reframe.detection import load_cv2

    h, w = gray.shape[:2]
    x0, y0 = int(max(0, box[0])), int(max(0, box[1]))
    x1, y1 = int(min(w, box[2])), int(min(h, box[3]))
    if x1 - x0 < 4 or y1 - y0 < 3:
        return None
    region = load_cv2().resize(gray[y0:y1, x0:x1], PATCH_SIZE, interpolation=1).astype(np.float32)
    std = float(region.std())
    return (region - float(region.mean())) / (std + 4.0)  # +4: flat regions do not amplify noise


class MouthMotion:
    """Keeps the previous patches of every track and measures movement frame to frame."""

    def __init__(self):
        self._previous: dict[int, tuple[int, object, object]] = {}

    def measure(self, key: int, frame_index: int, gray, det: Detection) -> float | None:
        regions = mouth_regions(det, gray.shape[1], gray.shape[0])
        if regions is None:
            return None
        mouth, ref = _patch(gray, regions[0]), _patch(gray, regions[1])
        if mouth is None or ref is None:
            return None
        previous = self._previous.get(key)
        self._previous[key] = (frame_index, mouth, ref)
        if previous is None or previous[0] != frame_index - 1:
            return None
        mouth_change = float(abs(mouth - previous[1]).mean())
        ref_change = float(abs(ref - previous[2]).mean())
        return max(0.0, mouth_change - ref_change)

"""Synthetic tracking data for reframe tests (no video or detector needed)."""

from ai_video_studio.reframe.model import Track, TrackingAnalysis, TrackSample


def make_track(track_id, kind, fn, duration, fps=5.0, w=1920, h=1080, confidence=0.9):
    """``fn(t) -> (cx, cy, bw, bh)`` in pixels, or ``None`` when the subject is not visible."""
    samples = []
    for i in range(int(duration * fps) + 1):
        t = i / fps
        box = fn(t)
        if box is None:
            continue
        cx, cy, bw, bh = box
        samples.append(TrackSample(t, (cx - bw / 2) / w, (cy - bh / 2) / h, bw / w, bh / h, confidence))
    return Track(track_id, kind, samples)


def make_analysis(tracks, duration=10.0, fps=5.0, w=1920, h=1080):
    return TrackingAnalysis(width=w, height=h, duration=duration, sample_fps=fps,
                            frames_analyzed=int(duration * fps) + 1, detector="test", tracks=tracks)


def face_at(cx, cy, size=120):
    return lambda t: (cx, cy, size, size * 1.2)

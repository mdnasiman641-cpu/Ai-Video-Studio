from ai_video_studio.reframe.model import Detection
from ai_video_studio.reframe.tracker import IouTracker, iou


def det(cx, cy, size=0.08, conf=0.9, kind="face"):
    return Detection(cx - size / 2, cy - size / 2, size, size, conf, kind)


def run(frames, fps=5.0, **kw):
    tracker = IouTracker(aspect=1.0, **kw)
    for i, detections in enumerate(frames):
        tracker.update(i / fps, detections)
    return tracker.finish()


def test_iou():
    assert iou((0, 0, 1, 1), (0, 0, 1, 1)) == 1
    assert iou((0, 0, 1, 1), (2, 2, 1, 1)) == 0
    assert abs(iou((0, 0, 2, 1), (1, 0, 2, 1)) - 1 / 3) < 1e-9


def test_one_moving_subject_keeps_one_id():
    tracks = run([[det(0.1 + 0.02 * i, 0.5)] for i in range(30)])
    assert len(tracks) == 1 and tracks[0].id == 1 and len(tracks[0].samples) == 30
    assert tracks[0].kind == "face" and abs(tracks[0].mean_confidence - 0.9) < 1e-9


def test_two_people_keep_their_ids_when_they_pass_each_other():
    frames = []
    for i in range(40):
        a = det(0.2 + 0.015 * i, 0.45)  # walks right
        b = det(0.8 - 0.015 * i, 0.55)  # walks left, crossing at i ~ 20
        frames.append([b, a] if i % 2 else [a, b])  # detector output order varies
    tracks = run(frames)
    assert len(tracks) == 2
    for track in tracks:
        xs = [s.cx for s in track.samples]
        assert len(xs) == 40
        steps = [b - a for a, b in zip(xs, xs[1:])]
        assert all(s > 0 for s in steps) or all(s < 0 for s in steps), "a track jumped to the other person"


def test_brief_disappearance_keeps_the_id_and_long_one_starts_a_new_track():
    frames = [[det(0.5, 0.5)] for _ in range(10)] + [[] for _ in range(4)] + [[det(0.52, 0.5)] for _ in range(10)]
    tracks = run(frames, max_missed_seconds=1.5)  # 0.8 s gap
    assert len(tracks) == 1 and len(tracks[0].samples) == 20
    frames = [[det(0.5, 0.5)] for _ in range(10)] + [[] for _ in range(15)] + [[det(0.52, 0.5)] for _ in range(10)]
    tracks = run(frames, max_missed_seconds=1.5)  # 3 s gap
    assert [t.id for t in tracks] == [1, 2]


def test_single_frame_false_detections_are_dropped_and_ids_are_compact():
    frames = [[det(0.5, 0.5)] for _ in range(10)]
    frames[3] = frames[3] + [det(0.1, 0.1, conf=0.61)]  # one-off false positive
    frames[7] = frames[7] + [det(0.9, 0.9, conf=0.62)]
    tracks = run(frames)
    assert [t.id for t in tracks] == [1]


def test_faces_and_bodies_are_tracked_separately():
    frames = [[det(0.5, 0.3, kind="face"), Detection(0.4, 0.2, 0.2, 0.7, 0.8, "person")] for _ in range(6)]
    tracks = run(frames)
    assert sorted(t.kind for t in tracks) == ["face", "person"]


def test_fast_motion_is_matched_by_prediction():
    # 0.1 frame widths per analysed frame: boxes of successive frames do not overlap.
    tracks = run([[det(0.05 + 0.1 * i, 0.5, size=0.06)] for i in range(9)])
    assert len(tracks) == 1 and len(tracks[0].samples) == 9


def test_far_away_detection_does_not_steal_a_track():
    frames = [[det(0.2, 0.5)] for _ in range(5)] + [[det(0.85, 0.5)] for _ in range(5)]
    tracks = run(frames)
    assert len(tracks) == 2

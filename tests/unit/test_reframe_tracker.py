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
    tracks = run(frames, max_missed_seconds=1.5)  # 0.8 s gap: never closed
    assert len(tracks) == 1 and len(tracks[0].samples) == 20
    # 3 s occlusion: the online track closes, but the pieces are re-linked (same place, same size).
    frames = [[det(0.5, 0.5)] for _ in range(10)] + [[] for _ in range(15)] + [[det(0.52, 0.5)] for _ in range(10)]
    tracks = run(frames, max_missed_seconds=1.5)
    assert len(tracks) == 1 and len(tracks[0].samples) == 20
    # Gone for 10 s: beyond the re-link window, so a new ID (identity is not guessed).
    frames = [[det(0.5, 0.5)] for _ in range(10)] + [[] for _ in range(50)] + [[det(0.52, 0.5)] for _ in range(10)]
    assert [t.id for t in run(frames, max_missed_seconds=1.5)] == [1, 2]


def test_relink_refuses_far_away_or_differently_sized_reappearance():
    far = [[det(0.1, 0.5)] for _ in range(10)] + [[] for _ in range(10)] + [[det(0.9, 0.5)] for _ in range(10)]
    assert len(run(far)) == 2  # 0.8 frame widths in 2 s for a small face: not plausible
    bigger = [[det(0.5, 0.5)] for _ in range(10)] + [[] for _ in range(10)] + [[det(0.5, 0.5, size=0.2)] for _ in range(10)]
    assert len(run(bigger)) == 2  # a much bigger face is someone closer to the camera


def test_relink_is_skipped_when_two_people_could_be_the_one_who_returned():
    frames = [[det(0.5, 0.5)] for _ in range(10)] + [[] for _ in range(10)]
    frames += [[det(0.47, 0.5), det(0.56, 0.5)] for _ in range(10)]  # two similar faces appear near
    tracks = run(frames)
    assert len(tracks) == 3 and len(tracks[0].samples) == 10


def test_appearance_prevents_matching_a_different_person():
    red = tuple([1.0] + [0.0] * 7)
    blue = tuple([0.0] * 7 + [1.0])
    a = Detection(0.45, 0.45, 0.08, 0.08, 0.9, "face", appearance=red)
    b = Detection(0.46, 0.45, 0.08, 0.08, 0.9, "face", appearance=blue)  # same place, different look
    tracks = run([[a]] * 5 + [[b]] * 5)
    assert len(tracks) == 2


def test_size_gate_keeps_a_foreground_face_separate():
    # The subject is hidden when someone close to the camera stands up exactly in front of them.
    frames = []
    for i in range(30):
        dets = [det(0.45, 0.4, size=0.05)] if not 12 <= i < 18 else []
        if 12 <= i < 18:
            dets.append(det(0.45 + 0.01 * (i - 12), 0.42, size=0.15))
        frames.append(dets)
    tracks = run(frames)
    subject = [t for t in tracks if t.samples[0].w < 0.1]
    assert len(subject) == 1 and all(s.w < 0.1 for s in subject[0].samples), "the big face took over the subject's ID"
    assert len(subject[0].samples) == 24  # re-linked after the occlusion


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

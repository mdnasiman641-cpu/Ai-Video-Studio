import math
import random

import pytest

from ai_video_studio.reframe.framing import (
    ASPECT_PRESETS,
    CropPath,
    compute_crop_path,
    crop_size,
    output_size_options,
    resolve_output_size,
)
from ai_video_studio.reframe.model import Detection, ReframeError, ReframeSettings
from ai_video_studio.reframe.tracker import IouTracker
from tests.reframe_helpers import face_at, make_analysis, make_track

W, H = 1920, 1080


def settings(**kw):
    base = dict(smoothing=0.0, dead_zone=0.0, caption_safe="off", padding=0.1)
    base.update(kw)
    return ReframeSettings(**base)


def centre_x(path, t):
    x, _y = path.at(t)
    return x + path.crop_w / 2


# -- geometry ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("src,preset,expected", [
    ((1920, 1080), "9:16", (608, 1080)),
    ((1920, 1080), "16:9", (1920, 1080)),
    ((1920, 1080), "1:1", (1080, 1080)),
    ((1920, 1080), "4:5", (864, 1080)),
    ((1920, 1080), "original", (1920, 1080)),
    ((1080, 1920), "16:9", (1080, 608)),
    ((1080, 1920), "9:16", (1080, 1920)),
    ((1080, 1920), "1:1", (1080, 1080)),
    ((1080, 1920), "4:5", (1080, 1350)),
    ((1280, 720), "9:16", (404, 720)),
])
def test_crop_size_for_every_preset(src, preset, expected):
    assert crop_size(*src, preset) == expected


@pytest.mark.parametrize("preset", [p for p in ASPECT_PRESETS if p != "original"])
@pytest.mark.parametrize("src", [(1920, 1080), (1080, 1920), (1001, 701), (720, 1280), (3840, 1600), (500, 500)])
@pytest.mark.parametrize("zoom", [1.0, 1.7, 3.0])
def test_crop_fits_keeps_aspect_and_is_even(src, preset, zoom):
    cw, ch = crop_size(*src, preset, zoom)
    rw, rh = ASPECT_PRESETS[preset]
    assert cw <= src[0] and ch <= src[1]
    assert cw % 2 == 0 and ch % 2 == 0
    assert abs(cw / ch - rw / rh) < 0.02  # only even rounding changes the ratio
    full = crop_size(*src, preset)
    assert abs(cw - full[0] / zoom) <= 2


def test_crop_size_rejects_bad_input():
    with pytest.raises(ReframeError):
        crop_size(1920, 1080, "21:9")
    with pytest.raises(ReframeError):
        crop_size(0, 1080, "9:16")


def test_output_sizes():
    assert output_size_options("9:16", W, H)[0] == (1080, 1920)
    assert output_size_options("4:5", W, H)[0] == (1080, 1350)
    assert output_size_options("original", 3840, 2160) == [(3840, 2160), (1920, 1080), (1280, 720)]
    assert output_size_options("original", 1081, 721)[0] == (1080, 720)
    assert resolve_output_size(ReframeSettings(preset="1:1"), W, H) == (1080, 1080)
    assert resolve_output_size(ReframeSettings(preset="9:16", output_width=720, output_height=1280), W, H) == (720, 1280)


# -- centre crop ------------------------------------------------------------------------------------
def test_center_mode_is_static_and_needs_no_analysis():
    path = compute_crop_path(None, ReframeSettings(tracking_mode="center"), W, H, 5.0)
    assert path.is_static and path.at(0) == path.at(4.9) == ((W - 608) // 2, 0)
    assert set(path.states) == {"center"}


def test_center_mode_offset_moves_and_is_clamped_to_the_frame():
    path = compute_crop_path(None, ReframeSettings(tracking_mode="center", offset_x=0.5), W, H, 2.0)
    assert path.at(1) == (656 + 304, 0)  # half a crop width to the right
    path = compute_crop_path(None, ReframeSettings(tracking_mode="center", preset="1:1", offset_x=0.5), W, H, 2.0)
    assert path.at(1)[0] == W - path.crop_w  # would leave the frame: clamped
    path = compute_crop_path(None, ReframeSettings(tracking_mode="center", preset="16:9", offset_x=-0.5, offset_y=0.3),
                             W, H, 2.0)
    assert path.at(1) == (0, 0)  # the crop is the whole frame, so it cannot move


# -- tracking-driven framing ------------------------------------------------------------------------
def test_tracking_requires_analysis_with_subjects():
    with pytest.raises(ReframeError, match="Analyse"):
        compute_crop_path(None, ReframeSettings(), W, H, 5.0)
    with pytest.raises(ReframeError, match="No faces"):
        compute_crop_path(make_analysis([]), ReframeSettings(), W, H, 5.0)
    analysis = make_analysis([make_track(1, "face", face_at(500, 400), 5)], duration=5)
    with pytest.raises(ReframeError, match="#7"):
        compute_crop_path(analysis, ReframeSettings(target=7), W, H, 5.0)


def test_static_subject_is_centred_not_the_frame():
    analysis = make_analysis([make_track(1, "face", face_at(500, 400), 10)])
    path = compute_crop_path(analysis, settings(), W, H, 10.0)
    for t in (0, 2.5, 9.9):
        assert abs(centre_x(path, t) - 500) <= 1
    assert path.tracked_fraction > 0.95 and not path.warnings
    # A fixed centre crop would be at x=656; the tracked crop is not.
    assert path.at(5)[0] < 300


def test_moving_subject_is_followed_without_lag():
    speed = 150  # px/s
    analysis = make_analysis([make_track(1, "face", lambda t: (300 + speed * t, 500, 120, 144), 10)])
    path = compute_crop_path(analysis, ReframeSettings(dead_zone=0.0, caption_safe="off"), W, H, 10.0)
    for t in (2.0, 5.0, 8.0):
        assert abs(centre_x(path, t) - (300 + speed * t)) < 16  # zero-phase smoothing: no trailing lag


def test_crop_always_inside_frame_for_random_motion():
    rng = random.Random(3)
    for preset in ("9:16", "1:1", "4:5", "16:9", "original"):
        for zoom in (1.0, 2.0):
            pos = [rng.uniform(0, W), rng.uniform(0, H)]

            def walk(t, pos=pos):
                pos[0] = min(W + 50, max(-50, pos[0] + rng.uniform(-200, 200)))
                pos[1] = min(H + 50, max(-50, pos[1] + rng.uniform(-100, 100)))
                return pos[0], pos[1], 150, 180

            analysis = make_analysis([make_track(1, "face", walk, 8)], duration=8)
            path = compute_crop_path(analysis, ReframeSettings(preset=preset, zoom=zoom), W, H, 8.0)
            for i in range(0, 801):
                x, y = path.at(i / 100)
                assert 0 <= x <= W - path.crop_w and 0 <= y <= H - path.crop_h


def test_dead_zone_ignores_jitter_and_smoothing_reduces_it():
    rng = random.Random(1)
    jitter = lambda t: (900 + rng.uniform(-15, 15), 500 + rng.uniform(-10, 10), 120, 144)  # noqa: E731
    analysis = make_analysis([make_track(1, "face", jitter, 10)])
    raw = compute_crop_path(analysis, settings(smoothing=0.0, dead_zone=0.0), W, H, 10.0)
    calm = compute_crop_path(analysis, ReframeSettings(caption_safe="off"), W, H, 10.0)  # defaults
    spread = lambda p: max(p.xs) - min(p.xs)  # noqa: E731
    assert spread(raw) > 20
    assert spread(calm) < 3


def test_speed_limit_and_controlled_movement():
    # The face steps 150 px to the right but stays inside the crop: the camera pans at the limit.
    analysis = make_analysis([make_track(1, "face", lambda t: (960 if t < 2 else 1110, 500, 60, 72), 6)], duration=6)
    s = settings(follow_speed=0.2, padding=0.0)
    path = compute_crop_path(analysis, s, W, H, 6.0)
    limit = 0.2 * path.crop_w * 0.1 + 1e-6
    steps = [abs(b - a) for a, b in zip(path.xs, path.xs[1:])]
    assert max(steps) <= limit
    assert abs(centre_x(path, 5.9) - 1110) <= 2  # it arrives eventually


def test_subject_is_kept_in_frame_even_when_speed_limit_is_low():
    analysis = make_analysis([make_track(1, "face", lambda t: (200 + 400 * t, 500, 100, 120), 4)], duration=4)
    path = compute_crop_path(analysis, settings(follow_speed=0.05, padding=0.0), W, H, 4.0)
    for t, x, state in zip(path.times, path.xs, path.states):
        cx = 200 + 400 * t
        if state == "tracked" and cx + 50 < W:
            assert x - 1 <= cx - 50 and cx + 50 <= x + path.crop_w + 1


def test_brief_occlusion_is_bridged():
    fn = lambda t: None if 4.0 < t < 4.6 else (400 + 100 * t, 500, 120, 144)  # noqa: E731
    analysis = make_analysis([make_track(1, "face", fn, 10)])
    path = compute_crop_path(analysis, settings(hold_seconds=1.0), W, H, 10.0)
    assert "lost" not in path.states
    assert abs(centre_x(path, 4.3) - (400 + 100 * 4.3)) < 15  # interpolated through the gap


def test_lost_subject_is_held_then_eased_back_to_centre():
    fn = lambda t: (400, 500, 120, 144) if t <= 3 else None  # noqa: E731
    analysis = make_analysis([make_track(1, "face", fn, 10)])
    s = settings(hold_seconds=1.0, transition_seconds=1.0)
    path = compute_crop_path(analysis, s, W, H, 10.0)
    assert path.state_at(2) == "tracked"
    assert path.state_at(3.6) == "held"
    assert abs(centre_x(path, 3.6) - 400) <= 1
    assert path.state_at(6) == "lost"
    assert abs(centre_x(path, 8) - W / 2) <= 1
    steps = [abs(b - a) for a, b in zip(path.xs, path.xs[1:])]
    assert max(steps) < 0.5 * (W / 2 - 400)  # an eased pan, not a cut
    assert any("only" in w for w in path.warnings)


def test_two_people_target_selection_and_no_jumping():
    left = make_track(1, "face", face_at(400, 500, 150), 10)
    right = make_track(2, "face", face_at(1500, 500, 100), 10)
    analysis = make_analysis([left, right])
    auto = compute_crop_path(analysis, settings(), W, H, 10.0)
    assert set(auto.followed) == {1}  # larger face chosen automatically, and kept
    chosen = compute_crop_path(analysis, settings(target=2), W, H, 10.0)
    assert abs(centre_x(chosen, 5) - 1500) <= 1 and set(chosen.followed) == {2}


def test_subject_change_is_a_controlled_transition():
    analysis = make_analysis([make_track(1, "face", face_at(400, 500), 10), make_track(2, "face", face_at(1500, 500), 10)])
    s = settings(target=1, switches=[[5.0, 2]], transition_seconds=1.0)
    path = compute_crop_path(analysis, s, W, H, 10.0)
    assert abs(centre_x(path, 4.8) - 400) <= 1
    assert abs(centre_x(path, 6.5) - 1500) <= 1
    mid = centre_x(path, 5.5)
    assert 600 < mid < 1300  # still panning half-way through the transition
    steps = [abs(b - a) for a, b in zip(path.xs, path.xs[1:])]
    assert max(steps) < 0.3 * (1500 - 400)
    assert path.followed_at(4) == 1 and path.followed_at(7) == 2


def _tracked(frames, duration, fps=5.0):
    """Run detections through the real tracker (with re-linking), like the analysis does."""
    tracker = IouTracker(aspect=W / H, max_missed_seconds=1.5)
    for i, dets in enumerate(frames):
        tracker.update(i / fps, dets)
    return make_analysis(tracker.finish(), duration=duration)


def _det(cx, cy, size=120):
    return Detection((cx - size / 2) / W, (cy - size * 0.6) / H, size / W, size * 1.2 / H, 0.9, "face")


def test_reappearing_subject_is_continued_but_not_ambiguous_ones():
    frames = [[_det(600, 500)] if i <= 15 or i >= 18 else [] for i in range(50)]  # 0.4 s gap: same track
    frames = [[_det(600, 500)] if i <= 15 else [_det(640, 510)] if i >= 28 else [] for i in range(50)]  # 2.4 s gap
    for f in frames:
        f.append(_det(1600, 500))
    analysis = _tracked(frames, 10.0)
    subject = [t for t in analysis.tracks if t.samples[0].cx * W < 1000]
    assert len(subject) == 1  # re-linked after the gap: one ID
    path = compute_crop_path(analysis, settings(tracking_mode="manual", target=subject[0].id), W, H, 10.0)
    assert path.followed_at(8) == subject[0].id and abs(centre_x(path, 8) - 640) <= 1

    ambiguous = [[_det(600, 500)] if i <= 15 else [_det(640, 510), _det(560, 520)] if i >= 28 else []
                 for i in range(50)]
    analysis = _tracked(ambiguous, 10.0)
    first = analysis.tracks[0]
    assert first.end < 4  # two candidates near the old position: not re-linked to either
    path = compute_crop_path(analysis, settings(tracking_mode="manual", target=first.id, hold_seconds=0.5), W, H, 10.0)
    assert path.state_at(8) == "lost"


def test_caption_safe_area_keeps_face_above_bottom_captions():
    analysis = make_analysis([make_track(1, "face", lambda t: (960, 300 + 40 * t, 120, 140), 10)])
    s = settings(preset="16:9", zoom=2.0, subject_y=0.75, caption_safe="bottom", caption_safe_height=0.3,
                 padding=0.1)
    path = compute_crop_path(analysis, s, W, H, 10.0)
    for t, y, state in zip(path.times, path.ys, path.states):
        if state == "tracked":
            face_bottom = 300 + 40 * t + 70 + 14
            assert face_bottom <= y + 0.7 * path.crop_h + 1
    top = compute_crop_path(analysis, settings(preset="16:9", zoom=2.0, subject_y=0.3, caption_safe="top",
                                               caption_safe_height=0.3), W, H, 10.0)
    for t, y, state in zip(top.times, top.ys, top.states):
        if state == "tracked" and y > 0:
            assert 300 + 40 * t - 70 - 14 >= y + 0.3 * top.crop_h - 1


def test_person_detections_frame_the_upper_body():
    body = make_track(1, "person", lambda t: (800, 600, 200, 800), 5)
    path = compute_crop_path(make_analysis([body], duration=5), settings(preset="16:9", zoom=2.0, subject_y=0.5,
                                                                      tracking_mode="auto_visible"),
                             W, H, 5.0)
    _x, y = path.at(2)
    anchor_centre = 600 - 400 + 0.2 * 800  # top of the box + half of its upper 40 %
    assert abs(y + path.crop_h / 2 - anchor_centre) <= 2


def test_portrait_source():
    analysis = make_analysis([make_track(1, "face", lambda t: (540, 300 + 100 * t, 150, 180), 8, w=1080, h=1920)],
                             duration=8, w=1080, h=1920)
    assert compute_crop_path(analysis, settings(preset="9:16"), 1080, 1920, 8.0).is_static
    square = compute_crop_path(analysis, settings(preset="1:1"), 1080, 1920, 8.0)
    assert square.ys[-1] > square.ys[0] + 300  # follows the subject downwards


@pytest.mark.parametrize("duration", [0.0, 0.05, 0.4])
def test_very_short_videos(duration):
    analysis = make_analysis([make_track(1, "face", face_at(500, 500), 1)], duration=1)
    path = compute_crop_path(analysis, settings(), W, H, duration)
    assert len(path.times) >= 2 and path.times[-1] == pytest.approx(duration)


def test_crop_path_keyframes_round_trip():
    analysis = make_analysis([make_track(1, "face", lambda t: (300 + 120 * t, 500, 120, 144), 6)], duration=6)
    path = compute_crop_path(analysis, ReframeSettings(), W, H, 6.0)
    copy = CropPath.from_keyframes(W, H, path.crop_w, path.crop_h, path.keyframes())
    for i in range(61):
        assert abs(copy.at(i / 10)[0] - path.at(i / 10)[0]) <= 1
    with pytest.raises(ReframeError):
        CropPath.from_keyframes(W, H, 4000, 100, path.keyframes())
    with pytest.raises(ReframeError):
        CropPath.from_keyframes(W, H, 600, 1080, [])


def test_interpolation_between_keyframes():
    path = CropPath(W, H, 606, 1080, [0.0, 1.0], [0.0, 100.0], [0.0, 0.0])
    assert path.at(0.5) == (50, 0)
    assert path.at(-1) == (0, 0) and path.at(5) == (100, 0)
    assert math.isclose(path.tracked_fraction, 0.0)

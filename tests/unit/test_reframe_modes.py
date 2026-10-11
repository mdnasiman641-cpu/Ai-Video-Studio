"""Tracking modes on synthetic data (no video, detector or audio model).

Tracks, mouth-movement series and speech activity are generated from formulas with a known
ground truth. These tests check the decision logic of each mode; they are NOT a validation of
active-speaker detection on real footage (see tests/integration/test_reframe_detectors_real.py
for the real-detector / real-VAD synthetic clips, and the README for what is unverified).
"""

import math

import pytest

from ai_video_studio.reframe.analysis import add_quality_notes
from ai_video_studio.reframe.export import sendcmd_script
from ai_video_studio.reframe.framing import (
    caption_band_from_style,
    compute_crop_path,
    effective_crop,
)
from ai_video_studio.reframe.model import (
    ReframeError,
    ReframeSettings,
    SpeechActivity,
    Track,
    TrackingAnalysis,
    TrackSample,
    reanalysis_reasons,
)
from ai_video_studio.captions.style import CaptionStyle

W, H = 1920, 1080
FPS = 15.0
RATE = 31.25


def syllables(t):
    """Speech-like loudness rhythm (about 4 syllables per second)."""
    return 0.5 + 0.45 * math.sin(2 * math.pi * 4 * t)


def noise(t, k=1.0):
    return 0.01 * (math.sin(17.3 * t * k) + math.sin(5.1 * t * k + 1))


def face_track(track_id, cx, duration, talking=lambda t: False, visible=lambda t: True, size=150,
               move=lambda t: 0.0, conf=0.9):
    samples = []
    for i in range(int(duration * FPS) + 1):
        t = i / FPS
        if not visible(t):
            continue
        x = cx + move(t)
        mouth = (0.05 + 0.6 * max(0.0, syllables(t) - 0.5)) if talking(t) else 0.03 + noise(t, track_id)
        samples.append(TrackSample(t, (x - size / 2) / W, (500 - size * 0.6) / H, size / W, size * 1.2 / H,
                                   conf, mouth))
    return Track(track_id, "face", samples)


def speech(duration, speaking=lambda t: False, loud=lambda t: 0.0):
    n = int(duration * RATE) + 1
    prob = [0.95 if speaking(i / RATE) else 0.02 for i in range(n)]
    energy = [syllables(i / RATE) if speaking(i / RATE) else loud(i / RATE) for i in range(n)]
    return SpeechActivity(RATE, prob, energy, "synthetic")


def analysis(tracks, duration, speech_activity=None, features=("faces", "mouth", "speech"), fps=FPS):
    return TrackingAnalysis(W, H, duration, fps, int(duration * fps) + 1, "synthetic", tracks,
                            features=list(features), speech=speech_activity)


def settings(**kw):
    base = dict(caption_safe="off", smoothing=0.3, dead_zone=0.0, transition_seconds=0.5)
    base.update(kw)
    return ReframeSettings(**base)


def followed(path, times):
    return [path.followed_at(t) for t in times]


def centre_x(path, t):
    return path.at(t)[0] + path.crop_w / 2


# -- active speaker ---------------------------------------------------------------------------------
def test_one_person_speaks_while_the_other_listens():
    a = face_track(1, 500, 10, talking=lambda t: True)
    b = face_track(2, 1400, 10)
    path = compute_crop_path(analysis([a, b], 10, speech(10, lambda t: True)),
                             settings(tracking_mode="active_speaker"), W, H, 10)
    assert set(followed(path, [2, 4, 6, 8, 9.5])) == {1}
    assert 2 not in path.followed
    assert path.speaker_score_at(5) > 0.5
    assert abs(centre_x(path, 5) - 500) < 5


def test_speaker_change_between_two_visible_people_is_confirmed_not_instant():
    a = face_track(1, 500, 14, talking=lambda t: t < 6)
    b = face_track(2, 1400, 14, talking=lambda t: t >= 7)
    sp = speech(14, lambda t: t < 6 or t >= 7)
    s = settings(tracking_mode="active_speaker", switch_seconds=1.0)
    path = compute_crop_path(analysis([a, b], 14, sp), s, W, H, 14)
    assert path.followed_at(5) == 1
    assert path.followed_at(6.6) == 1          # silence: no switching
    assert path.followed_at(7.4) == 1          # B started, evidence not yet confirmed
    assert path.followed_at(9.5) == 2
    assert abs(centre_x(path, 12) - 1400) < 5
    first_b = next(t for t, f in zip(path.times, path.followed) if f == 2)
    assert first_b >= 7.0 + 1.0 - 0.6          # roughly the confirmation time after B starts (window ±0.6 s)
    slow = compute_crop_path(analysis([a, b], 14, sp), settings(tracking_mode="active_speaker", switch_seconds=2.5),
                             W, H, 14)
    assert next(t for t, f in zip(slow.times, slow.followed) if f == 2) > first_b + 1.0


def test_silent_foreground_person_crossing_is_not_followed():
    a = face_track(1, 600, 10, talking=lambda t: True, size=110)
    passerby = face_track(3, 0, 10, visible=lambda t: 3 <= t <= 7, size=420, move=lambda t: 200 + 350 * (t - 3))
    path = compute_crop_path(analysis([a, passerby], 10, speech(10, lambda t: True)),
                             settings(tracking_mode="active_speaker"), W, H, 10)
    assert 3 not in path.followed
    assert set(followed(path, [2, 4, 5, 6, 8])) == {1}


def test_off_screen_narration_does_not_pick_a_visible_face():
    a = face_track(1, 500, 12)
    b = face_track(2, 1400, 12)
    path = compute_crop_path(analysis([a, b], 12, speech(12, lambda t: True)),
                             settings(tracking_mode="active_speaker", fallback="center"), W, H, 12)
    assert set(path.followed) == {None}
    assert set(path.states) == {"lost"}
    assert abs(centre_x(path, 6) - W / 2) < 2
    assert any("speaker could be identified" in w for w in path.warnings)


def test_narration_after_a_speaker_keeps_them_briefly_then_falls_back():
    a = face_track(1, 500, 14, talking=lambda t: t < 5)
    sp = speech(14, lambda t: True)  # narration continues after A stops talking
    path = compute_crop_path(analysis([a, face_track(2, 1400, 14)], 14, sp),
                             settings(tracking_mode="active_speaker", speaker_hold_seconds=3.0, fallback="center"),
                             W, H, 14)
    assert path.followed_at(4) == 1
    assert path.followed_at(7) == 1            # kept for the hold time
    assert path.followed_at(11) is None and path.state_at(11) == "lost"


def test_silence_and_background_music_never_name_a_speaker():
    a = face_track(1, 500, 10, talking=lambda t: True)  # mouth moves (e.g. singing along, chewing)
    music = speech(10, lambda t: False, loud=lambda t: 0.8)  # loud, but the VAD says no speech
    path = compute_crop_path(analysis([a], 10, music), settings(tracking_mode="active_speaker"), W, H, 10)
    assert set(path.followed) == {None}


def test_overlapping_speech_is_uncertain_and_keeps_the_current_speaker():
    a = face_track(1, 500, 12, talking=lambda t: True)
    b = face_track(2, 1400, 12, talking=lambda t: t >= 5)  # B talks over A from 5 s
    path = compute_crop_path(analysis([a, b], 12, speech(12, lambda t: True)),
                             settings(tracking_mode="active_speaker", speaker_hold_seconds=15), W, H, 12)
    assert path.followed_at(3) == 1
    assert set(followed(path, [6, 8, 10, 11.5])) == {1}  # no switch without clear evidence


def test_brief_occlusion_of_the_speaker_is_held_without_switching():
    a = face_track(1, 500, 10, talking=lambda t: True, visible=lambda t: not 4 <= t < 4.8)
    b = face_track(2, 1400, 10)
    path = compute_crop_path(analysis([a, b], 10, speech(10, lambda t: True)),
                             settings(tracking_mode="active_speaker", hold_seconds=1.0), W, H, 10)
    assert path.followed_at(4.4) == 1
    assert 2 not in path.followed


# -- speech-audio-driven ----------------------------------------------------------------------------
def test_speech_driven_follows_a_single_visible_person_and_holds_during_silence():
    a = face_track(1, 400, 10, move=lambda t: 80 * t)
    sp = speech(10, lambda t: t < 3 or t >= 6)
    path = compute_crop_path(analysis([a], 10, sp, features=("faces", "speech"), fps=5),
                             settings(tracking_mode="speech_audio"), W, H, 10)
    assert path.state_at(1) == "tracked" and path.state_at(4.5) == "silence"
    xs_silence = [x for t, x in zip(path.times, path.xs) if 3.6 <= t <= 5.8]
    assert max(xs_silence) - min(xs_silence) < 1   # the person moves, the framing does not
    assert abs(centre_x(path, 9) - (400 + 80 * 9)) < 30


def test_speech_driven_does_not_guess_between_several_visible_people():
    a, b = face_track(1, 500, 8), face_track(2, 1400, 8)
    sp = speech(8, lambda t: True)
    data = analysis([a, b], 8, sp, features=("faces", "speech"), fps=5)
    centred = compute_crop_path(data, settings(tracking_mode="speech_audio", fallback="center"), W, H, 8)
    assert set(centred.followed) == {None} and abs(centre_x(centred, 4) - W / 2) < 2
    group = compute_crop_path(data, settings(tracking_mode="speech_audio", fallback="group"), W, H, 8)
    assert abs(centre_x(group, 4) - (500 + 1400) / 2) < 5


def test_speech_driven_keeps_the_previous_person_with_hold_fallback():
    a = face_track(1, 500, 10)
    b = face_track(2, 1400, 10, visible=lambda t: t >= 4)  # a second person joins later
    data = analysis([a, b], 10, speech(10, lambda t: True), features=("faces", "speech"), fps=5)
    path = compute_crop_path(data, settings(tracking_mode="speech_audio", fallback="hold"), W, H, 10)
    assert path.followed_at(2) == 1 and path.followed_at(8) == 1


def test_speech_driven_without_faces_uses_only_the_fallback():
    data = analysis([], 6, speech(6, lambda t: True), features=("speech",), fps=5)
    path = compute_crop_path(data, settings(tracking_mode="speech_audio"), W, H, 6)
    assert set(path.followed) == {None}
    assert any("No faces" in n for n in path.notes)


# -- face / subject modes ---------------------------------------------------------------------------
def test_auto_face_sticks_to_the_main_face_when_a_bigger_one_appears():
    main = face_track(1, 500, 12, size=120)
    big = face_track(2, 1400, 12, visible=lambda t: t >= 3, size=400)
    body = Track(3, "person", [TrackSample(i / 5, 0.7, 0.1, 0.2, 0.8, 0.9) for i in range(60)])
    path = compute_crop_path(analysis([main, big, body], 12, features=("faces",)),
                             settings(tracking_mode="auto_face"), W, H, 12)
    assert set(path.followed) == {1}


def test_auto_face_accepts_a_manual_choice_but_not_a_body():
    a, b = face_track(1, 500, 6), face_track(2, 1400, 6)
    body = Track(3, "person", [TrackSample(i / 5, 0.7, 0.1, 0.2, 0.8, 0.9) for i in range(30)])
    data = analysis([a, b, body], 6, features=("faces",))
    assert set(compute_crop_path(data, settings(tracking_mode="auto_face", target=2), W, H, 6).followed) == {2}
    with pytest.raises(ReframeError, match="full-body"):
        compute_crop_path(data, settings(tracking_mode="auto_face", target=3), W, H, 6)


def test_manual_mode_needs_a_subject_and_never_switches_by_itself():
    a = face_track(1, 500, 10, visible=lambda t: t < 4)
    b = face_track(2, 1400, 10)
    data = analysis([a, b], 10, features=("faces",))
    with pytest.raises(ReframeError, match="needs a subject"):
        compute_crop_path(data, settings(tracking_mode="manual"), W, H, 10)
    path = compute_crop_path(data, settings(tracking_mode="manual", target=1, fallback="hold"), W, H, 10)
    assert 2 not in path.followed and path.state_at(8) == "lost"
    assert abs(centre_x(path, 8) - 500) < 5  # "hold" keeps the last framing
    switched = compute_crop_path(data, settings(tracking_mode="manual", target=1, switches=[[5.0, 2]]), W, H, 10)
    assert switched.followed_at(8) == 2


def test_automatic_visible_subject_moves_on_when_the_subject_is_gone():
    a = face_track(1, 500, 10, visible=lambda t: t < 4)
    b = face_track(2, 1400, 10)
    path = compute_crop_path(analysis([a, b], 10, features=("faces",)), settings(tracking_mode="auto_visible"),
                             W, H, 10)
    assert path.followed_at(2) in (1, 2) and path.followed_at(8) == 2


def test_center_mode_ignores_analysis():
    path = compute_crop_path(None, settings(tracking_mode="center"), W, H, 5)
    assert path.is_static and set(path.states) == {"center"}


# -- reanalysis and settings ------------------------------------------------------------------------
def test_reanalysis_is_required_when_the_analysis_lacks_what_the_mode_needs():
    faces_only = analysis([face_track(1, 500, 4)], 4, features=("faces",), fps=5)
    assert reanalysis_reasons(faces_only, settings(tracking_mode="auto_face")) == []
    assert reanalysis_reasons(faces_only, settings(tracking_mode="center")) == []
    for mode in ("active_speaker", "speech_audio"):
        reasons = reanalysis_reasons(faces_only, settings(tracking_mode=mode))
        assert reasons and "speech" in reasons[0]
        with pytest.raises(ReframeError, match="Reanalysis required"):
            compute_crop_path(faces_only, settings(tracking_mode=mode), W, H, 4)
    slow_mouth = analysis([face_track(1, 500, 4)], 4, speech(4), fps=5)
    assert any("10 frames" in r for r in reanalysis_reasons(slow_mouth, settings(tracking_mode="active_speaker")))
    strict = analysis([face_track(1, 500, 4)], 4, features=("faces",), fps=5)
    strict.face_threshold = 0.7
    assert reanalysis_reasons(strict, settings(confidence_threshold=0.5))
    assert not reanalysis_reasons(strict, settings(confidence_threshold=0.8))


def test_higher_confidence_threshold_filters_without_reanalysis():
    weak = face_track(2, 1400, 6, conf=0.62)
    data = analysis([face_track(1, 500, 6), weak], 6, features=("faces",))
    path = compute_crop_path(data, settings(tracking_mode="auto_visible", target=None, confidence_threshold=0.7),
                             W, H, 6)
    assert 2 not in path.followed


def test_project_settings_from_v0_3_0_are_migrated():
    assert ReframeSettings.from_dict({"mode": "center"}).tracking_mode == "center"
    assert ReframeSettings.from_dict({"mode": "track", "target": 3}).tracking_mode == "manual"
    assert ReframeSettings.from_dict({"mode": "track"}).tracking_mode == "auto_visible"
    old = TrackingAnalysis.from_dict({"width": 10, "height": 10, "duration": 1, "sample_fps": 5, "tracks": [
        {"id": 1, "kind": "person", "samples": [[0, 0.1, 0.1, 0.2, 0.2, 0.9]]}]})
    assert old.features == ["faces", "persons"] and old.speech is None


def test_analysis_features_and_rate_per_mode():
    assert settings(tracking_mode="active_speaker").analysis_features() == ["faces", "mouth", "speech"]
    assert settings(tracking_mode="active_speaker").analysis_rate() == 15.0
    assert "speech" in settings(tracking_mode="speech_audio").analysis_features()
    assert settings(tracking_mode="auto_face", detect_persons=False).analysis_features() == ["faces"]
    assert settings(tracking_mode="center").analysis_features() == []


# -- crop quality -----------------------------------------------------------------------------------
def test_maximum_upscale_limits_zoom_and_warns_when_the_source_is_too_small():
    cw, _ch, zoom, warnings, notes = effective_crop(settings(zoom=3.0, max_upscale=2.0), W, H)
    assert zoom == pytest.approx(2.0 * 608 / 1080, abs=0.01) and 1080 / cw <= 2.0 + 0.02
    assert notes and not warnings
    _cw, _ch, _z, warnings, _n = effective_crop(settings(max_upscale=1.5), W, H)
    assert warnings and "720×1280" in warnings[0]
    _cw, _ch, _z, warnings, _n = effective_crop(settings(max_upscale=1.5, output_width=720, output_height=1280), W, H)
    assert not warnings
    path = compute_crop_path(None, settings(tracking_mode="center", zoom=3.0), W, H, 2)
    assert path.upscale <= 2.0 + 0.02


def test_caption_band_comes_from_the_caption_style():
    position, fraction = caption_band_from_style(CaptionStyle())
    assert position == "bottom" and 0.15 < fraction < 0.3
    assert caption_band_from_style(CaptionStyle(vertical="top"))[0] == "top"
    assert caption_band_from_style(CaptionStyle(vertical="middle")) is None
    bigger = caption_band_from_style(CaptionStyle(font_size=120, max_lines=3))[1]
    assert bigger > fraction


def test_auto_caption_area_keeps_the_face_above_captions_when_possible():
    a = face_track(1, 960, 6, move=lambda t: 0)
    data = analysis([a], 6, features=("faces",))
    s = settings(tracking_mode="auto_face", preset="16:9", zoom=2.0, subject_y=0.8, caption_safe="auto")
    band = ("bottom", 0.3)
    path = compute_crop_path(data, s, W, H, 6, caption_band=band)
    _x, y = path.at(3)
    face_bottom = 500 - 150 * 0.6 + 150 * 1.2 * 1.35
    assert face_bottom <= y + 0.7 * path.crop_h + 2
    assert not path.warnings
    no_burn = compute_crop_path(data, settings(**{**s.to_dict(), "burn_captions": False}), W, H, 6, caption_band=band)
    assert no_burn.at(3)[1] != y  # without burned-in captions there is no band to avoid


def test_caption_conflict_on_a_full_height_crop_is_reported():
    low = Track(1, "face", [TrackSample(i / 5, 0.45, 0.78, 0.08, 0.15, 0.9) for i in range(31)])
    path = compute_crop_path(analysis([low], 6, features=("faces",)), settings(tracking_mode="auto_face", caption_safe="auto"),
                             W, H, 6, caption_band=("bottom", 0.25))
    assert any("caption area" in w and "full frame height" in w for w in path.warnings)


# -- reliability metrics ----------------------------------------------------------------------------
def test_partial_presence_is_not_reported_as_unreliable_tracking():
    partial = face_track(1, 500, 20, visible=lambda t: t >= 12)  # on screen 40 %, detected every frame
    data = analysis([partial], 20, features=("faces",))
    assert data.detection_rate(partial) == pytest.approx(1.0, abs=0.01)
    assert data.screen_share(partial) == pytest.approx(0.4, abs=0.02)
    add_quality_notes(data)
    assert not data.warnings and any("40%" in n for n in data.notes)
    flaky = face_track(2, 500, 20, visible=lambda t: int(t * FPS) % 3 == 0)  # detected in 1 of 3 frames
    data = analysis([flaky], 20, features=("faces",))
    add_quality_notes(data)
    assert any("while on" in w for w in data.warnings)


# -- preview / export consistency -------------------------------------------------------------------
def test_export_commands_match_the_preview_crop_at_every_frame():
    a = face_track(1, 300, 8, move=lambda t: 150 * t)
    path = compute_crop_path(analysis([a], 8, features=("faces",)), settings(tracking_mode="auto_face"), W, H, 8)
    script = sendcmd_script(path, 25, 8)
    commands = []
    for line in script.strip().splitlines():
        t, rest = line.split(" ", 1)
        x = int(rest.split(", ")[0].split()[-1])
        commands.append((float(t), x))
    for k in range(0, 200):
        t = k / 25
        sent = [x for at, x in commands if at <= t + 1e-9][-1]
        assert sent == path.at(t)[0], f"frame {k}: export {sent} != preview {path.at(t)[0]}"

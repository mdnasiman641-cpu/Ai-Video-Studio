"""Smart Cut: level parsing, silence detection, segment editing and undo/redo."""

import pytest

from ai_video_studio.smartcut.levels import FLOOR_DB, WINDOW, AudioLevels, LevelsParser, suggest_threshold
from ai_video_studio.smartcut.model import (
    EditHistory,
    SmartCutError,
    SmartCutSettings,
    SmartCutState,
    build_segments,
    detect_silences,
    detection_notes,
    keep_ranges,
    set_bounds,
    set_remove,
)


def levels_from(pattern: list[tuple[float, int]]) -> AudioLevels:
    """[(seconds, dB), ...] -> levels with 25 ms windows."""
    peak = []
    for seconds, db in pattern:
        peak += [db] * int(round(seconds / WINDOW))
    return AudioLevels(peak, list(peak), duration=len(peak) * WINDOW)


SPEECH, QUIET = -12, -60


def test_parser_reads_astats_output_including_minus_infinity_and_late_audio():
    lines = """frame:0    pts:16000   pts_time:1
lavfi.astats.Overall.Peak_level=-21.4
lavfi.astats.Overall.RMS_level=-24.1
frame:1    pts:16400   pts_time:1.025
lavfi.astats.Overall.Peak_level=-inf
lavfi.astats.Overall.RMS_level=-inf
frame:2    pts:16800   pts_time:1.05
lavfi.astats.Overall.Peak_level=nan
lavfi.astats.Overall.RMS_level=-130
""".splitlines()
    parser = LevelsParser()
    times = [parser.feed(line) for line in lines]
    assert [t for t in times if t is not None] == [1.0, 1.025, 1.05]
    # The audio starts 1 s late: the first 40 windows are filled with silence.
    assert parser.peak[:40] == [FLOOR_DB] * 40
    assert parser.peak[40:] == [-21, FLOOR_DB, FLOOR_DB]
    assert parser.rms[40:] == [-24, FLOOR_DB, FLOOR_DB]


def test_levels_round_trip_compactly():
    levels = AudioLevels([-3, -120, -45, 0], [-6, -120, -50, -1], duration=0.1, source_size=5, source_mtime=1.5)
    data = levels.to_dict()
    assert isinstance(data["peak"], str) and len(data["peak"]) < 20
    again = AudioLevels.from_dict(data)
    assert again.peak == levels.peak and again.rms == levels.rms and again.source_size == 5


def test_no_silence_keeps_everything():
    levels = levels_from([(5, SPEECH)])
    assert detect_silences(levels, SmartCutSettings()) == []
    segments = build_segments([], levels.duration)
    assert [(s.kind, s.start, s.end, s.remove) for s in segments] == [("speech", 0.0, 5.0, False)]
    assert "No silence" in detection_notes(levels, segments, SmartCutSettings())[0]


def test_multiple_silences_with_padding():
    levels = levels_from([(2, SPEECH), (1, QUIET), (2, SPEECH), (1.5, QUIET), (1, SPEECH)])
    settings = SmartCutSettings(pad_before=0.15, pad_after=0.25)
    assert detect_silences(levels, settings) == [(2.25, 2.85), (5.25, 6.35)]
    segments = build_segments(detect_silences(levels, settings), levels.duration)
    assert [(s.kind, s.start, s.end) for s in segments] == [
        ("speech", 0.0, 2.25), ("silence", 2.25, 2.85), ("speech", 2.85, 5.25),
        ("silence", 5.25, 6.35), ("speech", 6.35, 7.5)]
    assert keep_ranges(segments) == [(0.0, 2.25), (2.85, 5.25), (6.35, 7.5)]


def test_short_pauses_are_not_silence_and_edges_need_no_padding():
    levels = levels_from([(1, QUIET), (2, SPEECH), (0.4, QUIET), (2, SPEECH), (1, QUIET)])
    silences = detect_silences(levels, SmartCutSettings(min_silence=0.6))
    # Leading silence starts at 0 and trailing silence ends at the clip end (no padding there).
    assert silences == [(0.0, 0.85), (5.65, 6.4)]


def test_padding_can_consume_a_short_silence():
    levels = levels_from([(1, SPEECH), (0.6, QUIET), (1, SPEECH)])
    assert detect_silences(levels, SmartCutSettings(min_silence=0.5, pad_before=0.3, pad_after=0.3)) == []


def test_threshold_decides_what_is_quiet_and_low_volume_dialogue_is_kept():
    levels = levels_from([(1, -30), (1, -50), (1, -30)])
    assert detect_silences(levels, SmartCutSettings(threshold_db=-35, pad_before=0, pad_after=0)) == [(1.0, 2.0)]
    # With a threshold below the quiet dialogue level nothing is removed.
    assert detect_silences(levels, SmartCutSettings(threshold_db=-55)) == []


def test_min_keep_joins_a_short_sound_between_silences_and_notes_it():
    levels = levels_from([(1, SPEECH), (1, QUIET), (0.1, SPEECH), (1, QUIET), (1, SPEECH)])
    s = SmartCutSettings(min_silence=0.5, pad_before=0, pad_after=0, min_keep=0.3)
    segments = build_segments(detect_silences(levels, s), levels.duration, s.min_keep)
    assert [(x.kind, x.start, x.end) for x in segments] == [("speech", 0.0, 1.0), ("silence", 1.0, 3.1),
                                                           ("speech", 3.1, 4.1)]
    assert "short sound" in segments[1].note
    kept = build_segments(detect_silences(levels, s), levels.duration, 0.0)
    assert len(kept) == 5  # min_keep 0: the short sound stays a separate kept segment


def test_merge_gap_keeps_short_silences():
    levels = levels_from([(1, SPEECH), (0.8, QUIET), (1, SPEECH), (3, QUIET), (1, SPEECH)])
    s = SmartCutSettings(pad_before=0, pad_after=0, merge_gap=1.0)
    assert detect_silences(levels, s) == [(2.8, 5.8)]


def test_very_short_clip_and_silent_audio():
    tiny = levels_from([(0.1, SPEECH)])
    assert [s.kind for s in build_segments(detect_silences(tiny, SmartCutSettings()), tiny.duration)] == ["speech"]
    silent = AudioLevels([FLOOR_DB] * 80, [FLOOR_DB] * 80, duration=2.0)
    segments = build_segments(detect_silences(silent, SmartCutSettings()), silent.duration)
    assert [(s.kind, s.start, s.end) for s in segments] == [("silence", 0.0, 2.0)]
    assert "silent" in detection_notes(silent, segments, SmartCutSettings())[0]
    state = SmartCutState(levels=silent)
    state.detect()
    with pytest.raises(SmartCutError):
        state.apply()  # nothing would be left


def test_audio_shorter_than_video_counts_as_silence_at_the_end():
    levels = levels_from([(2, SPEECH)])
    levels.duration = 4.0
    assert detect_silences(levels, SmartCutSettings(pad_after=0)) == [(2.0, 4.0)]


def test_mostly_quiet_audio_suggests_a_lower_threshold():
    levels = levels_from([(1, -40), (9, -60)])
    segments = build_segments(detect_silences(levels, SmartCutSettings()), levels.duration)
    assert "lower threshold" in detection_notes(levels, segments, SmartCutSettings())[0]


def test_suggested_threshold_sits_between_noise_and_speech():
    levels = levels_from([(5, -50), (5, -15)])
    threshold, message = suggest_threshold(levels)
    assert -50 < threshold < -15 and "Background" in message
    music = levels_from([(5, -22), (5, -18)])
    _t, warning = suggest_threshold(music)
    assert "music" in warning


def test_settings_validation():
    with pytest.raises(SmartCutError):
        SmartCutSettings(threshold_db=0).validate()
    with pytest.raises(SmartCutError):
        SmartCutSettings(pad_before=-1).validate()
    assert SmartCutSettings.from_dict({"threshold_db": -40, "unknown": 1}).threshold_db == -40


def test_editing_bounds_keeps_a_partition_and_rejects_bad_times():
    segments = build_segments([(2.0, 3.0), (5.0, 6.0)], 8.0)
    silence = segments[1]
    set_bounds(segments, silence.id, 1.8, 3.2)
    assert (segments[0].end, segments[1].start, segments[1].end, segments[2].start) == (1.8, 1.8, 3.2, 3.2)
    for start, end in ((1.0, 1.02), (0.0, 3.0), (2.0, 7.9)):
        with pytest.raises(SmartCutError):
            set_bounds(segments, silence.id, start, end)
    with pytest.raises(SmartCutError):
        set_bounds(segments, segments[0].id, 0.5, 1.8)  # the first segment starts at 0


def test_keep_remove_and_undo_redo():
    segments = build_segments([(2.0, 3.0), (5.0, 6.0)], 8.0)
    history = EditHistory()
    history.record(segments)
    assert set_remove(segments, {segments[1].id}, False) == 1
    assert keep_ranges(segments) == [(0.0, 5.0), (6.0, 8.0)]
    segments = history.undo(segments)
    assert keep_ranges(segments) == [(0.0, 2.0), (3.0, 5.0), (6.0, 8.0)]
    assert history.can_redo
    segments = history.redo(segments)
    assert keep_ranges(segments) == [(0.0, 5.0), (6.0, 8.0)]
    history.record(segments)
    assert not history.can_redo  # a new edit clears redo


def test_state_apply_pending_and_round_trip():
    levels = levels_from([(2, SPEECH), (1, QUIET), (2, SPEECH)])
    state = SmartCutState(SmartCutSettings(pad_before=0, pad_after=0), levels)
    state.detect()
    assert state.applied is None and state.cut_ranges() is None and state.has_pending_changes
    assert state.apply() == [(0.0, 2.0), (3.0, 5.0)]
    assert state.cut_ranges() == [(0.0, 2.0), (3.0, 5.0)] and not state.has_pending_changes
    set_remove(state.segments, {state.segments[1].id}, False)
    assert state.has_pending_changes  # reviewed but not applied yet; exports still use the applied cuts
    again = SmartCutState.from_dict(state.to_dict())
    assert again.applied == state.applied and [s.remove for s in again.segments] == [False, False, False]
    assert again.levels.peak == levels.peak

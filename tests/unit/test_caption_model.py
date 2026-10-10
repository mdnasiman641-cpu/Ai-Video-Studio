import pytest

from ai_video_studio.captions.model import (
    CaptionError,
    CaptionSegment,
    CaptionTrack,
    Word,
    clean_text,
    find_overlaps,
    remap_to_cuts,
    segments_from_words,
    validate_times,
)


def _track(*items):
    return CaptionTrack(segments=[CaptionSegment(s, e, t) for s, e, t in items])


def _words(*items):
    return [Word(s, e, t) for s, e, t in items]


@pytest.mark.parametrize("start,end,duration,ok", [
    (0, 1, None, True),
    (1.5, 1.6, None, True),
    (-0.1, 1, None, False),
    (2, 2, None, False),
    (2, 1, None, False),
    (2, 2.01, None, False),       # shorter than the 50 ms minimum
    (11, 12, 10, False),          # starts after the end of the video
    ("a", 1, None, False),
    (float("nan"), 1, None, False),
])
def test_validate_times(start, end, duration, ok):
    assert (validate_times(start, end, duration) is None) is ok


def test_clean_text_keeps_line_breaks():
    assert clean_text("  hello   world \r\n\n  second  line ") == "hello world\nsecond line"


def test_add_uses_room_until_next_caption():
    track = _track((0, 1, "a"), (3, 4, "b"))
    seg = track.add(1.5, text=" new ")
    assert (seg.start, seg.end, seg.text) == (1.5, 3.0, "new")
    assert [s.text for s in track.segments] == ["a", "new", "b"]
    with pytest.raises(CaptionError, match="Not enough room"):
        track.add(2.99)
    with pytest.raises(CaptionError):
        track.add(5, 4)


def test_add_respects_video_duration():
    track = CaptionTrack()
    assert track.add(9.0, video_duration=10).end == 10


def test_delete_and_set_text():
    track = _track((0, 1, "a"), (1, 2, "b"))
    track.segments[0].words = _words((0, 1, " a"))
    track.set_text(track.segments[0].id, "changed")
    assert track.segments[0].text == "changed" and track.segments[0].words == []
    assert track.delete({track.segments[1].id}) == 1
    assert len(track.segments) == 1


def test_set_times_validates_and_resorts():
    track = _track((0, 1, "a"), (2, 3, "b"))
    b = track.segments[1]
    with pytest.raises(CaptionError):
        track.set_times(b.id, 3, 2)
    track.set_times(b.id, 0.0, 0.5)
    assert track.segments[0].id == b.id


def test_split_with_words_at_time():
    track = _track((0, 3, "one two three"))
    track.segments[0].words = _words((0, 0.9, " one"), (1.0, 1.9, " two"), (2.0, 3.0, " three"))
    a, b = track.split(track.segments[0].id, at_time=1.95)
    assert (a.text, a.start, a.end) == ("one two", 0, 1.95)
    assert (b.text, b.start, b.end) == ("three", 1.95, 3)
    assert len(a.words) == 2 and len(b.words) == 1


def test_split_at_text_cursor_places_time_proportionally():
    track = _track((10, 14, "hello world"))
    a, b = track.split(track.segments[0].id, text_index=5)
    assert (a.text, b.text) == ("hello", "world")
    assert a.end == b.start == pytest.approx(10 + 4 * 5 / 11, abs=0.001)


def test_split_without_words_uses_nearest_word_break():
    track = _track((0, 2, "alpha beta gamma delta"))
    a, b = track.split(track.segments[0].id, at_time=1.0)
    assert a.end == 1.0 and " " not in (a.text[-1], b.text[0])
    assert a.text + " " + b.text == "alpha beta gamma delta"


def test_split_errors():
    track = _track((0, 2, "word"))
    seg_id = track.segments[0].id
    with pytest.raises(CaptionError):
        track.split(seg_id, at_time=5)
    with pytest.raises(CaptionError):
        track.split(seg_id, text_index=0)
    track2 = _track((0, 2, "a b"))
    with pytest.raises(CaptionError, match="too close"):
        track2.split(track2.segments[0].id, at_time=0.01, text_index=1)


def test_merge_neighbours_only():
    track = _track((0, 1, "a"), (1, 2, "b"), (2, 3, "c"))
    ids = [s.id for s in track.segments]
    with pytest.raises(CaptionError, match="neighbouring"):
        track.merge([ids[0], ids[2]])
    with pytest.raises(CaptionError, match="at least two"):
        track.merge([ids[0]])
    merged = track.merge([ids[1], ids[0]])
    assert (merged.start, merged.end, merged.text) == (0, 2, "a b")
    assert len(track.segments) == 2


def test_merge_keeps_words_only_if_all_have_them():
    track = _track((0, 1, "a"), (1, 2, "b"))
    track.segments[0].words = _words((0, 1, " a"))
    assert track.merge([s.id for s in track.segments]).words == []


def test_active_at_and_overlaps():
    track = _track((0, 2, "a"), (1.5, 3, "b"), (5, 6, "c"))
    assert [s.text for s in track.active_at(1.6)] == ["a", "b"]
    assert [s.text for s in track.active_at(2.0)] == ["b"]
    assert track.active_at(4) == []
    assert len(find_overlaps(track.segments)) == 1


def test_round_trip_dict():
    track = _track((0, 1.23456, "x"))
    track.segments[0].words = _words((0, 1, " x"))
    track.pending_corrections = [{"id": "1", "original": "a", "suggested": "b"}]
    again = CaptionTrack.from_dict(track.to_dict())
    assert again.to_dict() == track.to_dict()
    assert again.segments[0].end == 1.235


def test_segments_from_words_limits():
    words = _words((0, 0.4, " Hello"), (0.5, 0.9, " there."), (1.0, 1.4, " This"), (1.5, 1.9, " is"),
                   (2.0, 2.4, " a"), (2.5, 2.9, " long"), (3.0, 3.4, " caption"), (6.0, 6.5, " After"),
                   (6.6, 7.0, " pause"))
    segs = segments_from_words(words, max_chars=20, max_duration=10, max_gap=1.0)
    texts = [s.text for s in segs]
    assert all(len(t) <= 20 for t in texts)
    assert texts[-1] == "After pause"           # long pause starts a new caption
    assert " ".join(texts).replace("  ", " ") == "Hello there. This is a long caption After pause"
    for a, b in zip(segs, segs[1:]):
        assert a.end <= b.start


def test_segments_from_words_max_duration():
    words = [Word(i * 1.0, i * 1.0 + 0.9, f" w{i}") for i in range(10)]
    segs = segments_from_words(words, max_chars=200, max_duration=3.0)
    assert all(s.duration <= 3.0 for s in segs)


def test_remap_to_cuts_basic():
    segs = [CaptionSegment(0, 1, "keep"), CaptionSegment(2, 3, "gone"), CaptionSegment(3.5, 5, "partial")]
    out = remap_to_cuts(segs, [(0, 1.5), (4, 6)])
    assert [(s.text, s.start, s.end) for s in out] == [("keep", 0, 1), ("partial", 1.5, 2.5)]


def test_remap_spanning_cut_removes_cut_words():
    seg = CaptionSegment(0, 4, "a b c d", words=_words((0, 0.9, " a"), (1, 1.9, " b"), (2, 2.9, " c"), (3, 3.9, " d")))
    out = remap_to_cuts([seg], [(0, 1.95), (3, 4)])
    assert out[0].text == "a b d"
    assert (out[0].start, out[0].end) == (0, 2.95)
    assert out[0].words[-1].start == pytest.approx(1.95)


def test_remap_rejects_overlapping_ranges():
    with pytest.raises(CaptionError):
        remap_to_cuts([], [(0, 2), (1, 3)])


def test_clock_format_and_parse():
    from ai_video_studio.captions.model import format_clock, parse_clock

    assert format_clock(75.5) == "01:15.500"
    assert format_clock(3725.5) == "1:02:05.500"
    assert parse_clock("01:15.500") == 75.5
    assert parse_clock("1:02:05,5") == 3725.5
    assert parse_clock(" 12.25 ") == 12.25
    assert parse_clock("90") == 90
    for bad in ("", "abc", "1:75", "-1", "1::2", "1:2:3:4", "1:60:00"):
        with pytest.raises(CaptionError):
            parse_clock(bad)

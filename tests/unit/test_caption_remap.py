"""Caption timing after Smart Cut: remapping onto the edited timeline."""

from ai_video_studio.captions.model import CaptionSegment, CaptionTrack, Word, find_overlaps, remap_to_cuts

KEEP = [(0.0, 2.0), (4.0, 6.0), (8.0, 10.0)]  # removes 2-4 and 6-8


def seg(start, end, text="x", sid=None, words=()):
    s = CaptionSegment(start, end, text, words=list(words))
    if sid:
        s.id = sid
    return s


def test_removed_captions_disappear_and_later_ones_shift():
    out = remap_to_cuts([seg(0.5, 1.5, "a", "a"), seg(2.5, 3.5, "gone", "g"), seg(4.5, 5.5, "b", "b"),
                         seg(8.5, 9.5, "c", "c")], KEEP)
    assert [(s.id, s.start, s.end, s.text) for s in out] == [
        ("a", 0.5, 1.5, "a"), ("b", 2.5, 3.5, "b"), ("c", 4.5, 5.5, "c")]


def test_caption_crossing_a_cut_is_clipped_and_never_negative():
    out = remap_to_cuts([seg(1.5, 4.5, "spans the cut")], KEEP)
    assert [(s.start, s.end) for s in out] == [(1.5, 2.5)]  # 1.5-2.0 kept, then 4.0-4.5 -> 2.0-2.5
    first = remap_to_cuts([seg(0.0, 0.3)], [(0.2, 5.0)])
    assert first[0].start == 0.0 and first[0].end == 0.1


def test_no_overlaps_are_created_and_order_is_valid():
    captions = [seg(i * 0.7, i * 0.7 + 0.7, f"c{i}") for i in range(14)]  # back to back, 0-9.8 s
    out = remap_to_cuts(captions, KEEP)
    assert not find_overlaps(out)
    assert all(s.end > s.start >= 0 for s in out)
    assert [s.start for s in out] == sorted(s.start for s in out)
    assert len({s.id for s in out}) == len(out)


def test_edited_text_and_style_survive_remapping():
    track = CaptionTrack([seg(4.2, 5.0, "original", "e", words=[Word(4.2, 4.6, " origi"), Word(4.6, 5.0, "nal")])])
    track.set_text("e", "Edited by hand")  # a manual edit drops word timings
    out = remap_to_cuts(track.segments, KEEP)
    assert out[0].text == "Edited by hand" and (out[0].start, out[0].end) == (2.2, 3.0)
    assert track.segments[0].start == 4.2  # the project's caption track itself is not changed


def test_words_cut_out_are_removed_from_the_text():
    s = seg(1.0, 5.0, "one two three", words=[Word(1.0, 1.8, " one"), Word(2.5, 3.5, " two"), Word(4.2, 5.0, " three")])
    out = remap_to_cuts([s], KEEP)
    assert out[0].text == "one three" and [w.text for w in out[0].words] == [" one", " three"]

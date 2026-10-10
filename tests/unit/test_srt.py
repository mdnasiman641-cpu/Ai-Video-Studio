import pytest

from ai_video_studio.captions.model import CaptionSegment, CaptionTrack
from ai_video_studio.captions.srt import (
    SrtError,
    format_timestamp,
    parse_srt,
    parse_timestamp,
    read_srt_file,
    to_srt,
    write_srt_file,
)


def test_timestamps():
    assert format_timestamp(0) == "00:00:00,000"
    assert format_timestamp(3725.5) == "01:02:05,500"
    assert format_timestamp(59.9996) == "00:01:00,000"
    assert parse_timestamp("01:02:05,500") == 3725.5
    assert parse_timestamp("00:00:01.5") == 1.5
    assert parse_timestamp("0:0:1") == 1.0
    with pytest.raises(ValueError):
        parse_timestamp("00:61:00,000")
    with pytest.raises(ValueError):
        format_timestamp(-1)


def test_parse_typical_file_with_quirks():
    text = (
        "\ufeff1\r\n00:00:01,000 --> 00:00:02,500\r\nHello <i>world</i>\r\n\r\n"
        "7\r\n00:00:03.000 --> 00:00:04,000 X1:0 X2:10\r\nTwo\r\nlines\r\n\r\n"
        "00:00:05,000 --> 00:00:06,000\r\nNo index\r\n\r\n"
        "garbage block\r\n\r\n"
        "4\r\n00:00:09,000 --> 00:00:08,000\r\nBackwards\r\n\r\n"
        "5\r\n00:00:10,000 --> 00:00:11,000\r\n\r\n"
    )
    result = parse_srt(text)
    assert [(s.start, s.end, s.text) for s in result.segments] == [
        (1.0, 2.5, "Hello world"),
        (3.0, 4.0, "Two\nlines"),
        (5.0, 6.0, "No index"),
    ]
    assert len(result.warnings) == 3  # garbage, backwards, (empty text is a separate block issue)


def test_round_trip():
    segs = [CaptionSegment(1.0, 2.0, "Hi"), CaptionSegment(0.0, 0.5, "First\nsecond line"),
            CaptionSegment(3600.25, 3601.0, "বাংলা ক্যাপশন")]
    out = to_srt(segs)
    assert out.startswith("1\n00:00:00,000 --> 00:00:00,500\nFirst\nsecond line\n")
    parsed = parse_srt(out).segments
    assert [(s.start, s.end, s.text) for s in parsed] == [(0.0, 0.5, "First\nsecond line"), (1.0, 2.0, "Hi"),
                                                       (3600.25, 3601.0, "বাংলা ক্যাপশন")]


def test_file_io_and_encodings(tmp_path):
    track = CaptionTrack(segments=[CaptionSegment(0, 1, "Ünïcode ✓")])
    path = write_srt_file(tmp_path / "out.txt", track)
    assert path.suffix == ".srt"
    assert read_srt_file(path).segments[0].text == "Ünïcode ✓"
    legacy = tmp_path / "legacy.srt"
    legacy.write_bytes("1\n00:00:00,000 --> 00:00:01,000\ncafé\n".encode("cp1252"))
    assert read_srt_file(legacy).segments[0].text == "café"
    utf16 = tmp_path / "u16.srt"
    utf16.write_text("1\n00:00:00,000 --> 00:00:01,000\nwide\n", encoding="utf-16")
    assert read_srt_file(utf16).segments[0].text == "wide"
    empty = tmp_path / "empty.srt"
    empty.write_text("nothing here")
    with pytest.raises(SrtError):
        read_srt_file(empty)

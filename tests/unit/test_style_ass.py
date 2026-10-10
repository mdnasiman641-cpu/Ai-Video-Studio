import pytest

from ai_video_studio.captions.ass import ass_color, ass_time, build_ass, escape_ass_text
from ai_video_studio.captions.model import CaptionSegment
from ai_video_studio.captions.style import BUILTIN_PRESETS, CaptionStyle, StyleError, wrap_text


def test_wrap_text():
    assert wrap_text("one two three four", 9) == ["one two", "three", "four"]
    assert wrap_text("keep\nbreak", 50) == ["keep", "break"]
    assert wrap_text("abcdefghij", 4) == ["abcd", "efgh", "ij"]
    lines = wrap_text("a b c d e f g h i j k l", 3, max_lines=2)
    assert len(lines) <= 2 and " ".join(lines) == "a b c d e f g h i j k l"


def test_style_validation_and_round_trip():
    style = CaptionStyle(font_size=70, text_color="#00ff00", vertical="top")
    style.validate()
    assert CaptionStyle.from_dict(style.to_dict()) == style
    with pytest.raises(StyleError):
        CaptionStyle(text_color="green").validate()
    with pytest.raises(StyleError):
        CaptionStyle(alignment="justify").validate()
    # damaged data falls back to defaults instead of failing to load the project
    assert CaptionStyle.from_dict({"font_size": 5000}) == CaptionStyle()
    assert CaptionStyle.from_dict({"font_size": "72", "unknown": 1}).font_size == 72
    for preset in BUILTIN_PRESETS.values():
        preset.validate()


def test_ass_helpers():
    assert ass_color("#112233") == "&H00332211"
    assert ass_color("#FFFFFF", 0) == "&HFFFFFFFF"
    assert ass_color("#000000", 60) == "&H66000000"
    assert ass_time(3725.456) == "1:02:05.46"
    assert ass_time(0) == "0:00:00.00"
    assert escape_ass_text(r"{\b1}x") == "\uff5b\u29f5b1\uff5dx"


def test_build_ass_scales_to_video_and_wraps():
    style = CaptionStyle(font_size=60, outline_width=3, max_chars_per_line=10, max_lines=3, margin_v=10,
                         alignment="left")
    segs = [CaptionSegment(1, 2.5, "hello there world"), CaptionSegment(0, 1, "first")]
    ass = build_ass(segs, style, 1080, 1920)
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass and "WrapStyle: 2" in ass
    style_line = next(l for l in ass.splitlines() if l.startswith("Style: Default"))
    fields = style_line[len("Style: "):].split(",")
    assert fields[2] == f"{60 * 1920 / 1080:.2f}"      # font size scaled to height
    assert fields[18] == "1"                          # bottom-left
    assert fields[21] == "192"                        # 10% of 1920
    events = [l for l in ass.splitlines() if l.startswith("Dialogue")]
    assert events[0].startswith("Dialogue: 1,0:00:00.00,0:00:01.00,Default")
    assert events[1].endswith(r"hello\Nthere\Nworld")
    assert "Style: Box" not in ass


def test_build_ass_background_adds_box_layer():
    ass = build_ass([CaptionSegment(0, 1, "x")], BUILTIN_PRESETS["Boxed"], 1920, 1080)
    box = next(l for l in ass.splitlines() if l.startswith("Style: Box")).split(",")
    assert box[15] == "3"                     # BorderStyle 3 = opaque box
    assert box[3] == "&HFF000000"             # box layer text is invisible
    events = [l for l in ass.splitlines() if l.startswith("Dialogue")]
    assert events[0].startswith("Dialogue: 0,") and ",Box," in events[0]
    assert events[1].startswith("Dialogue: 1,") and ",Default," in events[1]


def test_build_ass_rejects_invalid_style():
    with pytest.raises(StyleError):
        build_ass([], CaptionStyle(font_size=1), 1920, 1080)

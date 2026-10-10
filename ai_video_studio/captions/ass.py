"""Advanced SubStation Alpha (ASS) generation for FFmpeg/libass burn-in.

Lines are wrapped here (``WrapStyle: 2`` disables libass wrapping) so the burned-in
result uses the same line breaks as the live preview.
"""

from __future__ import annotations

from ai_video_studio.captions.model import CaptionSegment
from ai_video_studio.captions.style import CaptionStyle, wrap_text

_ALIGN = {
    ("bottom", "left"): 1, ("bottom", "center"): 2, ("bottom", "right"): 3,
    ("middle", "left"): 4, ("middle", "center"): 5, ("middle", "right"): 6,
    ("top", "left"): 7, ("top", "center"): 8, ("top", "right"): 9,
}


def ass_color(hex_color: str, opacity_percent: float = 100) -> str:
    """``#RRGGBB`` + opacity → ``&HAABBGGRR`` (ASS alpha: 00 = opaque, FF = transparent)."""
    r, g, b = hex_color[1:3], hex_color[3:5], hex_color[5:7]
    alpha = round(255 * (1 - max(0.0, min(100.0, opacity_percent)) / 100))
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_time(seconds: float) -> str:
    """Seconds → ``H:MM:SS.cc``."""
    cs = int(round(max(0.0, seconds) * 100))
    hours, rem = divmod(cs, 360_000)
    minutes, rem = divmod(rem, 6000)
    secs, cs = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def escape_ass_text(text: str) -> str:
    # Braces start override blocks and backslashes start tags in ASS; replace them with
    # look-alike characters so user text is always shown literally.
    return text.replace("\\", "⧵").replace("{", "｛").replace("}", "｝")


def caption_lines(text: str, style: CaptionStyle) -> list[str]:
    return wrap_text(text, style.max_chars_per_line, style.max_lines)


def _style_line(name: str, style: CaptionStyle, width: int, height: int, *, box: bool) -> str:
    k = style.scale(height)
    if box:
        primary = "&HFF000000"  # invisible text: this layer only draws the box
        outline_color = ass_color(style.background_color, style.background_opacity)
        back = "&HFF000000"
        border_style, outline, shadow = 3, style.background_padding * k, 0
    else:
        primary = ass_color(style.text_color)
        outline_color = ass_color(style.outline_color)
        back = ass_color(style.shadow_color, style.shadow_opacity)
        border_style, outline, shadow = 1, style.outline_width * k, style.shadow_offset * k
    margin_h = round(width * style.margin_h / 100)
    margin_v = round(height * style.margin_v / 100)
    fields = [
        name, style.font_family, f"{style.font_size * k:.2f}",
        primary, primary, outline_color, back,
        "-1" if style.bold else "0", "-1" if style.italic else "0", "0", "0",
        "100", "100", "0", "0",
        str(border_style), f"{outline:.2f}", f"{shadow:.2f}",
        str(_ALIGN[(style.vertical, style.alignment)]),
        str(margin_h), str(margin_h), str(margin_v), "1",
    ]
    return "Style: " + ",".join(fields)


def build_ass(segments: list[CaptionSegment], style: CaptionStyle, width: int, height: int,
              title: str = "AI Video Studio captions") -> str:
    style.validate()
    header = [
        "[Script Info]",
        f"Title: {title}",
        "ScriptType: v4.00+",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        _style_line("Default", style, width, height, box=False),
    ]
    if style.background:
        header.append(_style_line("Box", style, width, height, box=True))
    header += ["", "[Events]",
               "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    events = []
    for seg in sorted(segments, key=lambda s: (s.start, s.end)):
        text = r"\N".join(escape_ass_text(line) for line in caption_lines(seg.text, style))
        if not text:
            continue
        start, end = ass_time(seg.start), ass_time(seg.end)
        if style.background:
            events.append(f"Dialogue: 0,{start},{end},Box,,0,0,0,,{text}")
        events.append(f"Dialogue: 1,{start},{end},Default,,0,0,0,,{text}")
    return "\n".join(header + events) + "\n"

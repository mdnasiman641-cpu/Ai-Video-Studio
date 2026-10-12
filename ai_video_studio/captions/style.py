"""Caption style, built-in presets and line wrapping (no Qt).

Sizes are given in pixels for a 1080-pixel-tall frame and are scaled to the
actual video height, so a style looks the same on 1080p, 720p or vertical video.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, fields

from ai_video_studio.core.errors import StudioError

REFERENCE_HEIGHT = 1080
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

ALIGNMENTS = ("left", "center", "right")
VERTICAL_POSITIONS = ("bottom", "middle", "top")


class StyleError(StudioError):
    pass


@dataclass
class CaptionStyle:
    font_family: str = "Arial"
    font_size: int = 64
    bold: bool = True
    italic: bool = False
    text_color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline_width: float = 3.0
    shadow_color: str = "#000000"
    shadow_opacity: int = 60       # percent
    shadow_offset: float = 2.0
    alignment: str = "center"      # left | center | right
    vertical: str = "bottom"       # bottom | middle | top
    margin_v: float = 8.0          # percent of video height (ignored for "middle")
    margin_h: float = 5.0          # percent of video width
    background: bool = False
    background_color: str = "#000000"
    background_opacity: int = 60   # percent
    background_padding: float = 12.0
    max_chars_per_line: int = 32
    max_lines: int = 2
    fade_in_ms: int = 0            # caption animation: fade in/out (ASS \fad), 0 = off
    fade_out_ms: int = 0

    def validate(self) -> None:
        errors = []
        if not self.font_family.strip():
            errors.append("Font is required")
        if not 8 <= self.font_size <= 300:
            errors.append("Font size must be between 8 and 300")
        for name in ("text_color", "outline_color", "shadow_color", "background_color"):
            if not _HEX.match(getattr(self, name)):
                errors.append(f"{name.replace('_', ' ').capitalize()} must look like #RRGGBB")
        if not 0 <= self.outline_width <= 20:
            errors.append("Outline must be between 0 and 20")
        if not 0 <= self.shadow_offset <= 20:
            errors.append("Shadow offset must be between 0 and 20")
        for name in ("shadow_opacity", "background_opacity"):
            if not 0 <= getattr(self, name) <= 100:
                errors.append(f"{name.replace('_', ' ').capitalize()} must be 0–100")
        if self.alignment not in ALIGNMENTS:
            errors.append("Alignment must be left, center or right")
        if self.vertical not in VERTICAL_POSITIONS:
            errors.append("Position must be bottom, middle or top")
        if not 0 <= self.margin_v <= 45 or not 0 <= self.margin_h <= 45:
            errors.append("Margins must be between 0% and 45%")
        if not 0 <= self.background_padding <= 100:
            errors.append("Background padding must be between 0 and 100")
        if not 8 <= self.max_chars_per_line <= 120:
            errors.append("Characters per line must be between 8 and 120")
        if not 1 <= self.max_lines <= 5:
            errors.append("Lines must be between 1 and 5")
        if not 0 <= self.fade_in_ms <= 2000 or not 0 <= self.fade_out_ms <= 2000:
            errors.append("Fade-in and fade-out must be between 0 and 2000 ms")
        if errors:
            raise StyleError("; ".join(errors))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None, fallback: "CaptionStyle | None" = None) -> "CaptionStyle":
        """Read a stored style; damaged data gives ``fallback`` (default: the default style)."""
        if not data:
            return fallback or cls()
        known = {f.name: f.type for f in fields(cls)}
        values = {}
        defaults = cls()
        for key, value in data.items():
            if key not in known:
                continue
            default = getattr(defaults, key)
            try:
                values[key] = type(default)(value)
            except (TypeError, ValueError):
                continue
        try:
            style = cls(**values)
            style.validate()
        except (StyleError, TypeError, ValueError):
            return fallback or cls()  # damaged style data falls back rather than breaking the project
        return style

    def scale(self, video_height: int) -> float:
        return (video_height or REFERENCE_HEIGHT) / REFERENCE_HEIGHT


BUILTIN_PRESETS: dict[str, CaptionStyle] = {
    "Classic White": CaptionStyle(),
    "Bold Yellow": CaptionStyle(font_size=72, text_color="#FFE600", outline_width=4, margin_v=12),
    "Boxed": CaptionStyle(outline_width=0, shadow_offset=0, background=True, background_opacity=70),
    "Vertical Center": CaptionStyle(font_size=80, vertical="middle", outline_width=5, max_chars_per_line=20),
    "Subtle Top": CaptionStyle(font_size=48, bold=False, vertical="top", margin_v=6, outline_width=2),
    "Bold Green": CaptionStyle(font_size=72, text_color="#39FF6A", outline_width=4, margin_v=12),
    "Red Outline": CaptionStyle(font_size=68, outline_color="#E0182D", outline_width=5, shadow_opacity=40),
    "Clean Minimal": CaptionStyle(font_size=52, bold=False, outline_width=0, shadow_offset=2, shadow_opacity=70,
                                  max_chars_per_line=40),
    "Highlight Style": CaptionStyle(font_size=64, text_color="#111111", outline_width=0, shadow_offset=0,
                                    background=True, background_color="#FFE600", background_opacity=100,
                                    background_padding=10),
    "Social Video": CaptionStyle(font_size=84, vertical="middle", outline_width=6, shadow_offset=3,
                                 max_chars_per_line=18, fade_in_ms=150, fade_out_ms=150),
    "Large Readable": CaptionStyle(font_size=90, outline_width=5, background=True, background_opacity=55,
                                   max_chars_per_line=24, margin_v=10),
}


def wrap_text(text: str, max_chars: int, max_lines: int | None = None) -> list[str]:
    """Greedy word wrap that keeps existing line breaks.

    Words longer than ``max_chars`` (or scripts without spaces) are split by characters.
    If ``max_lines`` is set and exceeded, the line width is increased until it fits,
    so no text is ever dropped.
    """
    def wrap(width: int) -> list[str]:
        lines: list[str] = []
        for paragraph in text.split("\n"):
            current = ""
            for word in paragraph.split():
                while len(word) > width:
                    if current:
                        lines.append(current)
                        current = ""
                    lines.append(word[:width])
                    word = word[width:]
                if not current:
                    current = word
                elif len(current) + 1 + len(word) <= width:
                    current += " " + word
                else:
                    lines.append(current)
                    current = word
            if current:
                lines.append(current)
        return lines

    width = max(1, max_chars)
    lines = wrap(width)
    if max_lines:
        while len(lines) > max_lines and width < len(text):
            width += 4
            lines = wrap(width)
    return lines

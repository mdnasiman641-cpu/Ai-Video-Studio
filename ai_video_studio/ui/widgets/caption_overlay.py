"""Live caption preview painted with QPainter.

It follows the same rules as the ASS file used for export (see captions/ass.py):
identical line breaks, sizes relative to a 1080-pixel-tall frame, the same margins,
outline, shadow and box. Font rasterisation differs slightly between Qt and libass,
so the Captions page also offers an exact single-frame render made by FFmpeg.
"""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QTextLayout,
)

from ai_video_studio.captions.ass import caption_lines
from ai_video_studio.captions.style import REFERENCE_HEIGHT, CaptionStyle


def _color(hex_color: str, opacity_percent: float = 100) -> QColor:
    color = QColor(hex_color)
    color.setAlphaF(max(0.0, min(1.0, opacity_percent / 100)))
    return color


@lru_cache(maxsize=64)
def _em_ratio(family: str, bold: bool, italic: bool) -> float:
    """libass/VSFilter font size is the line height (ascent + descent), not the em size."""
    font = QFont(family)
    font.setPixelSize(100)
    font.setBold(bold)
    font.setItalic(italic)
    metrics = QFontMetricsF(font)
    height = metrics.ascent() + metrics.descent()
    return height / 100 if height > 0 else 1.0


def shaped_text_path(font: QFont, x: float, baseline: float, text: str) -> tuple[QPainterPath, float]:
    """Outline path of ``text`` with full script shaping (needed for Bengali, Hindi, Arabic...).

    ``QPainterPath.addText`` does not apply complex-script shaping, so glyphs are taken
    from a QTextLayout instead. Returns ``(path, advance_width)``.
    """
    layout = QTextLayout(text, font)
    layout.beginLayout()
    line = layout.createLine()
    if not line.isValid():
        layout.endLayout()
        return QPainterPath(), 0.0
    line.setLineWidth(1e6)
    line.setPosition(QPointF(0, 0))
    layout.endLayout()
    path = QPainterPath()
    ascent = line.ascent()
    for run in layout.glyphRuns():
        raw = run.rawFont()
        for index, pos in zip(run.glyphIndexes(), run.positions()):
            glyph = raw.pathForGlyph(index)
            path.addPath(glyph.translated(x + pos.x(), baseline - ascent + pos.y()))
    return path, line.naturalTextWidth()


def make_font(style: CaptionStyle, line_height_px: float) -> QFont:
    font = QFont(style.font_family)
    font.setBold(style.bold)
    font.setItalic(style.italic)
    font.setPixelSize(max(1, round(line_height_px / _em_ratio(style.font_family, style.bold, style.italic))))
    return font


def paint_captions(painter: QPainter, rect: QRectF, texts: list[str], style: CaptionStyle) -> None:
    """Paint ``texts`` (active captions, oldest first) inside the on-screen video ``rect``."""
    if not texts or rect.height() <= 0:
        return
    k = rect.height() / REFERENCE_HEIGHT           # style units -> screen pixels
    font = make_font(style, style.font_size * k)
    metrics = QFontMetricsF(font)
    line_h = metrics.ascent() + metrics.descent()
    margin_h = rect.width() * style.margin_h / 100
    margin_v = rect.height() * style.margin_v / 100
    left, right = rect.left() + margin_h, rect.right() - margin_h

    lines: list[str] = []
    for text in texts:
        lines.extend(caption_lines(text, style))
    if not lines:
        return
    block_h = line_h * len(lines)
    if style.vertical == "bottom":
        top = rect.bottom() - margin_v - block_h
    elif style.vertical == "top":
        top = rect.top() + margin_v
    else:
        top = rect.center().y() - block_h / 2

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    painter.setClipRect(rect)
    outline = style.outline_width * k
    shadow = style.shadow_offset * k
    pad = style.background_padding * k

    for i, line in enumerate(lines):
        baseline = top + i * line_h + metrics.ascent()
        path, width = shaped_text_path(font, 0, baseline, line)
        if style.alignment == "left":
            x = left
        elif style.alignment == "right":
            x = right - width
        else:
            x = (left + right - width) / 2
        path.translate(x, 0)

        if style.background:
            box = QRectF(x - pad, top + i * line_h - pad, width + 2 * pad, line_h + 2 * pad)
            painter.fillRect(box, _color(style.background_color, style.background_opacity))

        stroke = None
        if outline > 0:
            stroker = QPainterPathStroker(QPen())
            stroker.setWidth(outline * 2)
            stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
            stroke = stroker.createStroke(path).united(path)
        if shadow > 0 and style.shadow_opacity > 0:
            shape = stroke if stroke is not None else path
            painter.fillPath(shape.translated(shadow, shadow), _color(style.shadow_color, style.shadow_opacity))
        if stroke is not None:
            painter.fillPath(stroke, _color(style.outline_color))
        painter.fillPath(path, _color(style.text_color))
    painter.restore()


def paint_segments(painter: QPainter, rect: QRectF, segments, default: CaptionStyle) -> None:
    """Draw caption segments; a caption with its own style is drawn with it, as in the export."""
    groups: dict[str, tuple[CaptionStyle, list[str]]] = {}
    for seg in segments:
        key = repr(sorted(seg.style.items())) if seg.style else ""
        groups.setdefault(key, (seg.style_or(default), []))[1].append(seg.text)
    for style, texts in groups.values():
        paint_captions(painter, rect, texts, style)

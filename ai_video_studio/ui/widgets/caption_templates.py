"""Visual grid of caption templates (built-in and saved caption presets).

Each tile is drawn with the same painter as the live caption preview, from the preset's real
style values (colours, outline, shadow, box, position, alignment, bold/italic). Sizes are
enlarged so the sample is readable in a small tile. Works offline: nothing is downloaded.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import QButtonGroup, QGridLayout, QScrollArea, QToolButton, QVBoxLayout, QWidget

from ai_video_studio.captions.style import CaptionStyle
from ai_video_studio.ui.widgets.caption_overlay import paint_captions

TILE = QSize(176, 99)
SAMPLE_TEXT = "Your caption here"
PREVIEW_ENLARGE = 2.4  # the tile is small; enlarge text so the style is visible


def template_pixmap(style: CaptionStyle, size: QSize = TILE) -> QPixmap:
    pixmap = QPixmap(size)
    painter = QPainter(pixmap)
    gradient = QLinearGradient(0, 0, size.width(), size.height())
    gradient.setColorAt(0, QColor("#3a4a5c"))
    gradient.setColorAt(1, QColor("#1b222b"))
    painter.fillRect(pixmap.rect(), gradient)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    k = PREVIEW_ENLARGE
    shown = CaptionStyle.from_dict(style.to_dict() | {
        "font_size": min(300, round(style.font_size * k)), "outline_width": min(20.0, style.outline_width * k),
        "shadow_offset": min(20.0, style.shadow_offset * k), "background_padding": min(100.0, style.background_padding * k),
        "max_chars_per_line": 120, "max_lines": 1}, fallback=style)
    paint_captions(painter, QRectF(0, 0, size.width(), size.height()), [SAMPLE_TEXT], shown)
    painter.end()
    return pixmap


class CaptionTemplateGrid(QWidget):
    chosen = Signal(str)  # preset name

    def __init__(self, columns: int = 3, parent: QWidget | None = None):
        super().__init__(parent)
        self.columns = columns
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, QToolButton] = {}
        self._inner = QWidget()
        self._grid = QGridLayout(self._inner)
        self._grid.setSpacing(6)
        scroll = QScrollArea()
        scroll.setWidget(self._inner)
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(TILE.height() * 2 + 70)
        scroll.setMaximumHeight(TILE.height() * 2 + 90)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

    def set_templates(self, entries: list[tuple[str, CaptionStyle, bool]], selected: str | None = None) -> None:
        """``entries``: ``(name, style, built_in)``."""
        for button in self.buttons.values():
            self.group.removeButton(button)
            button.deleteLater()
        self.buttons.clear()
        for n, (name, style, builtin) in enumerate(entries):
            button = QToolButton()
            button.setCheckable(True)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setIcon(QIcon(template_pixmap(style)))
            button.setIconSize(TILE)
            button.setText(name)
            button.setToolTip(f"{name} ({'built-in' if builtin else 'your preset'}). Click to choose it, then apply "
                              "it to the selected captions or to all captions.")
            button.clicked.connect(lambda _checked=False, n=name: self.chosen.emit(n))
            self.group.addButton(button)
            self.buttons[name] = button
            self._grid.addWidget(button, n // self.columns, n % self.columns)
        self.select(selected)

    def select(self, name: str | None) -> None:
        button = self.buttons.get(name or "")
        if button is not None:
            button.setChecked(True)

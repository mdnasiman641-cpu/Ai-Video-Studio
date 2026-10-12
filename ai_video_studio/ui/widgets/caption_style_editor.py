"""Form for editing a :class:`CaptionStyle`."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFontComboBox,
    QFormLayout,
    QHBoxLayout,
    QPushButton,
    QSpinBox,
    QWidget,
)

from ai_video_studio.captions.style import CaptionStyle


class ColorButton(QPushButton):
    color_changed = Signal(str)

    def __init__(self, color: str = "#FFFFFF"):
        super().__init__()
        self.setFixedWidth(90)
        self.clicked.connect(self._pick)
        self.set_color(color)

    def color(self) -> str:
        return self._color

    def set_color(self, color: str) -> None:
        self._color = QColor(color).name().upper()
        text = "#000000" if QColor(self._color).lightness() > 140 else "#FFFFFF"
        self.setText(self._color)
        self.setStyleSheet(f"background-color: {self._color}; color: {text}; border: 1px solid #555;")

    def _pick(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._color), self, "Choose colour")
        if chosen.isValid():
            self.set_color(chosen.name())
            self.color_changed.emit(self._color)


class CaptionStyleEditor(QWidget):
    style_changed = Signal(object)  # CaptionStyle

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._loading = False

        self.font = QFontComboBox()
        self.size = QSpinBox()
        self.size.setRange(8, 300)
        self.size.setToolTip("Line height in pixels on a 1080-pixel-tall frame (scaled for other sizes)")
        self.bold = QCheckBox("Bold")
        self.italic = QCheckBox("Italic")
        self.text_color = ColorButton()
        self.outline_color = ColorButton("#000000")
        self.outline_width = QDoubleSpinBox()
        self.outline_width.setRange(0, 20)
        self.outline_width.setSingleStep(0.5)
        self.shadow_color = ColorButton("#000000")
        self.shadow_offset = QDoubleSpinBox()
        self.shadow_offset.setRange(0, 20)
        self.shadow_offset.setSingleStep(0.5)
        self.shadow_opacity = QSpinBox()
        self.shadow_opacity.setRange(0, 100)
        self.shadow_opacity.setSuffix(" %")
        self.alignment = QComboBox()
        self.alignment.addItems(["left", "center", "right"])
        self.vertical = QComboBox()
        self.vertical.addItems(["bottom", "middle", "top"])
        self.margin_v = QDoubleSpinBox()
        self.margin_v.setRange(0, 45)
        self.margin_v.setSuffix(" %")
        self.margin_h = QDoubleSpinBox()
        self.margin_h.setRange(0, 45)
        self.margin_h.setSuffix(" %")
        self.background = QCheckBox("Show background box")
        self.background_color = ColorButton("#000000")
        self.background_opacity = QSpinBox()
        self.background_opacity.setRange(0, 100)
        self.background_opacity.setSuffix(" %")
        self.background_padding = QDoubleSpinBox()
        self.background_padding.setRange(0, 100)
        self.max_chars = QSpinBox()
        self.max_chars.setRange(8, 120)
        self.max_lines = QSpinBox()
        self.max_lines.setRange(1, 5)
        self.fade_in, self.fade_out = QSpinBox(), QSpinBox()
        for w in (self.fade_in, self.fade_out):
            w.setRange(0, 2000)
            w.setSingleStep(50)
            w.setSuffix(" ms")
        fade_row = QHBoxLayout()
        fade_row.addWidget(self.fade_in)
        fade_row.addWidget(self.fade_out)

        font_row = QHBoxLayout()
        font_row.addWidget(self.bold)
        font_row.addWidget(self.italic)
        font_row.addStretch(1)
        outline_row = QHBoxLayout()
        outline_row.addWidget(self.outline_color)
        outline_row.addWidget(self.outline_width)
        shadow_row = QHBoxLayout()
        shadow_row.addWidget(self.shadow_color)
        shadow_row.addWidget(self.shadow_offset)
        shadow_row.addWidget(self.shadow_opacity)
        bg_row = QHBoxLayout()
        bg_row.addWidget(self.background_color)
        bg_row.addWidget(self.background_opacity)
        bg_row.addWidget(self.background_padding)

        form = QFormLayout(self)
        form.addRow("Font:", self.font)
        form.addRow("Size:", self.size)
        form.addRow("", font_row)
        form.addRow("Text colour:", self.text_color)
        form.addRow("Outline (colour, width):", outline_row)
        form.addRow("Shadow (colour, offset, opacity):", shadow_row)
        form.addRow("Alignment:", self.alignment)
        form.addRow("Position:", self.vertical)
        form.addRow("Vertical margin:", self.margin_v)
        form.addRow("Side margins:", self.margin_h)
        form.addRow("", self.background)
        form.addRow("Box (colour, opacity, padding):", bg_row)
        form.addRow("Characters per line:", self.max_chars)
        form.addRow("Max lines:", self.max_lines)
        form.addRow("Fade in / out:", fade_row)

        for w in (self.size, self.shadow_opacity, self.background_opacity, self.max_chars, self.max_lines,
                  self.fade_in, self.fade_out):
            w.valueChanged.connect(self._emit)
        for w in (self.outline_width, self.shadow_offset, self.margin_v, self.margin_h, self.background_padding):
            w.valueChanged.connect(self._emit)
        for w in (self.bold, self.italic, self.background):
            w.toggled.connect(self._emit)
        for w in (self.alignment, self.vertical):
            w.currentTextChanged.connect(self._emit)
        for w in (self.text_color, self.outline_color, self.shadow_color, self.background_color):
            w.color_changed.connect(self._emit)
        self.font.currentFontChanged.connect(self._on_font)
        self._family = CaptionStyle().font_family
        self.set_style(CaptionStyle())

    def set_style(self, style: CaptionStyle) -> None:
        self._loading = True
        try:
            self._family = style.font_family
            self.font.setCurrentFont(QFont(style.font_family))
            self.size.setValue(style.font_size)
            self.bold.setChecked(style.bold)
            self.italic.setChecked(style.italic)
            self.text_color.set_color(style.text_color)
            self.outline_color.set_color(style.outline_color)
            self.outline_width.setValue(style.outline_width)
            self.shadow_color.set_color(style.shadow_color)
            self.shadow_offset.setValue(style.shadow_offset)
            self.shadow_opacity.setValue(style.shadow_opacity)
            self.alignment.setCurrentText(style.alignment)
            self.vertical.setCurrentText(style.vertical)
            self.margin_v.setValue(style.margin_v)
            self.margin_h.setValue(style.margin_h)
            self.background.setChecked(style.background)
            self.background_color.set_color(style.background_color)
            self.background_opacity.setValue(style.background_opacity)
            self.background_padding.setValue(style.background_padding)
            self.max_chars.setValue(style.max_chars_per_line)
            self.max_lines.setValue(style.max_lines)
            self.fade_in.setValue(style.fade_in_ms)
            self.fade_out.setValue(style.fade_out_ms)
        finally:
            self._loading = False

    def style(self) -> CaptionStyle:  # noqa: A003 - mirrors the data it edits
        # Keep the stored family even if it is not installed here (the combo would show a substitute).
        return CaptionStyle(
            font_family=self._family,
            font_size=self.size.value(),
            bold=self.bold.isChecked(),
            italic=self.italic.isChecked(),
            text_color=self.text_color.color(),
            outline_color=self.outline_color.color(),
            outline_width=self.outline_width.value(),
            shadow_color=self.shadow_color.color(),
            shadow_opacity=self.shadow_opacity.value(),
            shadow_offset=self.shadow_offset.value(),
            alignment=self.alignment.currentText(),
            vertical=self.vertical.currentText(),
            margin_v=self.margin_v.value(),
            margin_h=self.margin_h.value(),
            background=self.background.isChecked(),
            background_color=self.background_color.color(),
            background_opacity=self.background_opacity.value(),
            background_padding=self.background_padding.value(),
            max_chars_per_line=self.max_chars.value(),
            max_lines=self.max_lines.value(),
            fade_in_ms=self.fade_in.value(),
            fade_out_ms=self.fade_out.value(),
        )

    def _on_font(self, font: QFont) -> None:
        if not self._loading:
            self._family = font.family()
            self._emit()

    def _emit(self, *_args) -> None:
        if not self._loading:
            self.style_changed.emit(self.style())

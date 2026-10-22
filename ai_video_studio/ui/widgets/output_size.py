"""Output size picker: aspect ratio × resolution, or a custom width × height (Bulk Processing).

It edits ``ResizeSettings.preset`` / ``width`` / ``height`` / ``target_short`` through
``render.layout.settings_for_size``, the same values the export uses, and shows the resulting
size for a sample source so the user sees exactly what will be rendered.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QSpinBox, QWidget

from ai_video_studio.core.errors import StudioError
from ai_video_studio.render.layout import (
    ASPECTS,
    MAX_H,
    MAX_W,
    MIN_SIZE,
    RESOLUTIONS,
    ResizeSettings,
    aspect_and_resolution,
    aspect_text,
    settings_for_size,
)


class OutputSizePicker(QWidget):
    changed = Signal()

    def __init__(self, allow_original: bool = True, parent: QWidget | None = None):
        super().__init__(parent)
        self._filling = False
        self.aspect = QComboBox()
        for key, (label, _ratio) in ASPECTS.items():
            if allow_original or key != "original":
                self.aspect.addItem(label, key)
        self.resolution = QComboBox()
        self.width_spin, self.height_spin = QSpinBox(), QSpinBox()
        for box, limit in ((self.width_spin, MAX_W), (self.height_spin, MAX_H)):
            box.setRange(MIN_SIZE, limit)
            box.setSingleStep(2)
            box.setKeyboardTracking(False)
            box.setToolTip("Even numbers only: H.264/H.265 in 4:2:0 need an even width and height.")
        self.lock = QCheckBox("Lock aspect ratio")
        self.lock.setToolTip("Changing the width changes the height to keep the current ratio (and the other way).")
        self.result = QLabel()
        self.result.setObjectName("Muted")
        dims = QHBoxLayout()
        dims.addWidget(self.width_spin)
        dims.addWidget(QLabel("×"))
        dims.addWidget(self.height_spin)
        dims.addWidget(self.lock)
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.addRow("Aspect ratio:", self.aspect)
        form.addRow("Resolution:", self.resolution)
        form.addRow("Custom size:", dims)
        form.addRow(self.result)
        self.aspect.currentIndexChanged.connect(self._on_aspect)
        self.resolution.currentIndexChanged.connect(self._emit)
        self.width_spin.valueChanged.connect(lambda v: self._on_dimension(self.width_spin, v))
        self.height_spin.valueChanged.connect(lambda v: self._on_dimension(self.height_spin, v))
        self._ratio = 16 / 9
        self._fill_resolutions()
        self._update()

    def _fill_resolutions(self) -> None:
        current = self.resolution.currentData()
        blocked = self.resolution.blockSignals(True)
        self.resolution.clear()
        for value, label in RESOLUTIONS.items():
            if value or self.aspect.currentData() == "original":
                self.resolution.addItem(label, value)
        self.resolution.setCurrentIndex(max(0, self.resolution.findData(current if current is not None else 1080)))
        if self.resolution.currentData() is None or self.resolution.findData(current) < 0:
            self.resolution.setCurrentIndex(max(0, self.resolution.findData(1080)))
        self.resolution.blockSignals(blocked)

    def _on_aspect(self, *_args) -> None:
        self._fill_resolutions()
        self._emit()

    def _on_dimension(self, box: QSpinBox, value: int) -> None:
        if value % 2:
            box.setValue(value + 1)  # even sizes only
            return
        if self.lock.isChecked() and not self._filling:
            other = self.height_spin if box is self.width_spin else self.width_spin
            target = value / self._ratio if box is self.width_spin else value * self._ratio
            self._filling = True
            other.setValue(max(MIN_SIZE, int(round(target / 2)) * 2))
            self._filling = False
        elif not self._filling:
            self._ratio = self.width_spin.value() / max(1, self.height_spin.value())
        self._emit()

    def _emit(self, *_args) -> None:
        self._update()
        if not self._filling:
            self.changed.emit()

    def _update(self) -> None:
        custom = self.aspect.currentData() == "custom"
        self.width_spin.setEnabled(custom)
        self.height_spin.setEnabled(custom)
        self.lock.setEnabled(custom)
        self.resolution.setEnabled(not custom)
        try:
            s = ResizeSettings()
            self.apply_to(s)
            if self.aspect.currentData() == "original":
                example = s.output_size(1920, 1080)
                text = ("Each video keeps its own aspect ratio" + (f" at {RESOLUTIONS[s.target_short]}" if
                                                                   s.target_short else " and resolution")
                        + f" (a 1920×1080 source becomes {example[0]}×{example[1]}).")
            else:
                w, h = s.output_size(1920, 1080)
                text = f"Output: {w} × {h} ({aspect_text(w, h)})"
        except StudioError as exc:
            text = f"⚠ {exc}"
        self.result.setText(text)

    # -- values --------------------------------------------------------------------
    def apply_to(self, settings: ResizeSettings) -> None:
        settings_for_size(settings, self.aspect.currentData(), int(self.resolution.currentData() or 0),
                          self.width_spin.value(), self.height_spin.value())

    def set_from(self, settings: ResizeSettings) -> None:
        aspect, short = aspect_and_resolution(settings)
        if self.aspect.findData(aspect) < 0:
            aspect = "custom"
        self._filling = True
        try:
            self.aspect.setCurrentIndex(self.aspect.findData(aspect))
            self._fill_resolutions()
            self.resolution.setCurrentIndex(max(0, self.resolution.findData(short)))
            w, h = settings.output_size(1920, 1080) if settings.preset != "original" else (1920, 1080)
            self.width_spin.setValue(int(w))
            self.height_spin.setValue(int(h))
            self._ratio = w / max(1, h)
        finally:
            self._filling = False
        self._update()

    def size_text(self) -> str:
        return self.result.text()

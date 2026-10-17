"""Colour grading and video filter controls (Bulk Processing).

Every control edits a value of ``render.color.ColorSettings``; the export turns those values into
FFmpeg filters (see that module), so nothing here only changes the look of the page.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio.core.errors import StudioError
from ai_video_studio.render.color import ADJUSTMENTS, FILTERS, GRADE_PRESETS, MANUAL, ColorSettings
from ai_video_studio.ui.widgets.cards import TEAL
from ai_video_studio.ui.widgets.common import muted

BUILTIN = "builtin:"
CUSTOM = "custom:"


class ValueSlider(QWidget):
    """A slider with its numeric value next to it."""

    valueChanged = Signal(int)  # noqa: N815 - Qt naming

    def __init__(self, low: int, high: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(low, high)
        self.spin = QSpinBox()
        self.spin.setRange(low, high)
        self.spin.setKeyboardTracking(False)
        self.spin.setFixedWidth(64)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.slider, 1)
        row.addWidget(self.spin)
        self.slider.valueChanged.connect(self.spin.setValue)
        self.spin.valueChanged.connect(self.slider.setValue)
        self.slider.valueChanged.connect(self.valueChanged)

    def value(self) -> int:
        return self.slider.value()

    def setValue(self, value: int) -> None:  # noqa: N802 - Qt naming
        self.slider.setValue(int(round(value)))


class ColorGradeForm(QWidget):
    """Look (built-in or the user's own), manual adjustments, reset and custom presets."""

    changed = Signal()

    def __init__(self, store=None, parent: QWidget | None = None):
        super().__init__(parent)
        self.store = store
        self._filling = False
        self._preset, self._name = "original", ""
        self.look = QComboBox()
        self.look.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.sliders: dict[str, ValueSlider] = {}
        self.active = QLabel()
        self.active.setWordWrap(True)
        self.active.setStyleSheet(f"color: {TEAL};")
        self.reset_button = QPushButton("Reset")
        self.save_button = QPushButton("Save Preset…")
        self.update_button = QPushButton("Update")
        self.delete_button = QPushButton("Delete")
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.addRow("Look:", self.look)
        form.addRow(self.active)
        for name in MANUAL:
            label, low, high = ADJUSTMENTS[name]
            self.sliders[name] = ValueSlider(int(low), int(high))
            form.addRow(f"{label.replace('&', '&&')}:", self.sliders[name])  # "&" is not a shortcut
            self.sliders[name].valueChanged.connect(self._on_slider)
        buttons = QGridLayout()
        for n, b in enumerate((self.reset_button, self.save_button, self.update_button, self.delete_button)):
            buttons.addWidget(b, n // 2, n % 2)
        form.addRow(buttons)
        form.addRow(muted("Looks set the sliders; moving a slider makes the look Custom. Black & White, sepia and "
                          "glow come from the looks and filters."))
        self.look.activated.connect(self._on_look)
        self.reset_button.clicked.connect(self.reset)
        self.save_button.clicked.connect(self.save_preset)
        self.update_button.clicked.connect(self.update_preset)
        self.delete_button.clicked.connect(self.delete_preset)
        self.fill_looks()

    # -- looks -----------------------------------------------------------------------
    def fill_looks(self, select: str | None = None) -> None:
        blocked = self.look.blockSignals(True)
        self.look.clear()
        for key, (label, _v) in GRADE_PRESETS.items():
            self.look.addItem(label, BUILTIN + key)
        for name in (self.store.names() if self.store else []):
            self.look.addItem(f"★ {name}", CUSTOM + name)
        self.look.setCurrentIndex(max(0, self.look.findData(select or self._key())))
        self.look.blockSignals(blocked)
        self._update()

    def _key(self) -> str:
        return CUSTOM + self._name if self._name else BUILTIN + self._preset

    def _on_look(self, *_args) -> None:
        key = self.look.currentData() or ""
        if key.startswith(CUSTOM):
            try:
                values = self.store.get(key[len(CUSTOM):])
            except StudioError as exc:
                QMessageBox.warning(self, "Colour Preset", str(exc))
                return
            self._preset, self._name = "custom", key[len(CUSTOM):]
        else:
            self._preset, self._name = key[len(BUILTIN):], ""
            values = dict(GRADE_PRESETS[self._preset][1])
        self._set_values(values)
        self._emit()

    def _set_values(self, values: dict[str, float]) -> None:
        self._filling = True
        try:
            for name, slider in self.sliders.items():
                slider.setValue(values.get(name, 0))
        finally:
            self._filling = False
        self._hidden = {k: v for k, v in values.items() if k not in MANUAL}  # mono/sepia/glow of a look

    def _on_slider(self, *_args) -> None:
        if self._filling:
            return
        if self._preset != "custom" or self._name:
            self._preset, self._name = "custom", ""
            self.look.setCurrentIndex(self.look.findData(BUILTIN + "custom"))
        self._emit()

    def _emit(self) -> None:
        self._update()
        if not self._filling:
            self.changed.emit()

    def _update(self) -> None:
        if self._name:
            what = f"{self._name} (your preset)"
        else:
            what = GRADE_PRESETS[self._preset][0] + (" (built-in look)" if self._preset not in ("custom",) else
                                                     " adjustments")
        changed = [ADJUSTMENTS[k][0] for k, v in self.values().items() if v]
        self.active.setText(f"Active: {what}" + (f" — {', '.join(changed)}" if changed else " — no changes"))
        custom = bool(self._name)
        self.update_button.setEnabled(custom)
        self.delete_button.setEnabled(custom)
        self.save_button.setEnabled(self.store is not None)

    # -- values ----------------------------------------------------------------------
    def values(self) -> dict[str, float]:
        values = dict(getattr(self, "_hidden", {}))
        values.update({name: float(s.value()) for name, s in self.sliders.items() if s.value()})
        return values

    def set_settings(self, color: ColorSettings) -> None:
        self._preset, self._name = color.preset, color.name
        if color.name and self.store and color.name not in self.store.names():
            self._name = ""  # the preset was deleted; the values stay
        self._set_values(color.values)
        self.fill_looks()

    def apply_to(self, color: ColorSettings) -> None:
        color.preset, color.name, color.values = self._preset, self._name, self.values()

    def reset(self) -> None:
        self._preset, self._name = "original", ""
        self._set_values({})
        self.look.setCurrentIndex(self.look.findData(BUILTIN + "original"))
        self._emit()

    # -- custom presets ------------------------------------------------------------------
    def save_preset(self) -> str | None:
        name, ok = QInputDialog.getText(self, "Save Colour Preset", "Preset name:")
        if not ok:
            return None
        return self._store(name)

    def _store(self, name: str) -> str | None:
        try:
            if name.strip() in (self.store.names() if self.store else []) and QMessageBox.question(
                    self, "Save Colour Preset", f"Replace your preset “{name.strip()}”?") != \
                    QMessageBox.StandardButton.Yes:
                return None
            name = self.store.save(name, self.values())
        except StudioError as exc:
            QMessageBox.warning(self, "Save Colour Preset", str(exc))
            return None
        self._preset, self._name = "custom", name
        self.fill_looks(CUSTOM + name)
        self._emit()
        return name

    def update_preset(self) -> None:
        if self._name:
            try:
                self.store.save(self._name, self.values())
            except StudioError as exc:
                QMessageBox.warning(self, "Update Colour Preset", str(exc))

    def delete_preset(self) -> None:
        if not self._name or QMessageBox.question(
                self, "Delete Colour Preset", f"Delete your colour preset “{self._name}”? Built-in looks are not "
                "affected.") != QMessageBox.StandardButton.Yes:
            return
        self.store.delete(self._name)
        self._preset, self._name = "custom", ""
        self.fill_looks()
        self._emit()


class FilterPicker(QWidget):
    """Video filter tiles, intensity 0–100 % and Reset Filter."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, QToolButton] = {}
        grid = QGridLayout()
        grid.setSpacing(4)
        for n, (key, (label, _v)) in enumerate(FILTERS.items()):
            b = QToolButton()
            b.setText(label.replace("&", "&&"))
            b.setCheckable(True)
            b.setMinimumWidth(96)
            b.setStyleSheet(f"QToolButton:checked {{ background: {TEAL}; color: #0b1418; font-weight: 600; }}")
            self.group.addButton(b)
            self.buttons[key] = b
            grid.addWidget(b, n // 3, n % 3)
            b.clicked.connect(self._emit)
        self.intensity = ValueSlider(0, 100)
        self.intensity.setValue(100)
        self.reset_button = QPushButton("Reset Filter")
        self.current = QLabel()
        self.current.setStyleSheet(f"color: {TEAL};")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(grid)
        row = QFormLayout()
        row.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        row.addRow("Intensity (%):", self.intensity)
        layout.addLayout(row)
        layout.addWidget(self.current)
        layout.addWidget(self.reset_button)
        self.intensity.valueChanged.connect(self._emit)
        self.reset_button.clicked.connect(self.reset)
        self.buttons["none"].setChecked(True)
        self._update()

    def selected(self) -> str:
        return next((k for k, b in self.buttons.items() if b.isChecked()), "none")

    def set_filter(self, key: str, intensity: int) -> None:
        blocked = self.intensity.blockSignals(True)
        self.buttons.get(key, self.buttons["none"]).setChecked(True)
        self.intensity.setValue(intensity)
        self.intensity.blockSignals(blocked)
        self._update()

    def reset(self) -> None:
        self.set_filter("none", 100)
        self.changed.emit()

    def _update(self) -> None:
        key = self.selected()
        self.intensity.setEnabled(key != "none")
        self.current.setText("Filter: none" if key == "none" else
                             f"Filter: {FILTERS[key][0]} at {self.intensity.value()}%"
                             + (" (no change at 0%)" if self.intensity.value() == 0 else ""))

    def _emit(self, *_args) -> None:
        self._update()
        self.changed.emit()

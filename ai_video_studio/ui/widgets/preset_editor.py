"""Editor for a batch export preset (shared batch settings or one video's override)."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio.core.errors import StudioError
from ai_video_studio.reframe.model import TRACKING_MODES
from ai_video_studio.render.batch import BATCH_RESIZE_MODES, BATCH_TRACKING_MODES, OPERATIONS, RenderPreset
from ai_video_studio.render.layout import MODES, PRESETS, ResizeSettings
from ai_video_studio.render.naming import TEMPLATE_FIELDS
from ai_video_studio.ui.widgets.common import muted
from ai_video_studio.ui.widgets.output_settings import OutputSettingsForm


class PresetEditor(QWidget):
    changed = Signal()

    def __init__(self, preset: RenderPreset | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self._filling = False
        self._background = "#000000"
        self.operation = QComboBox()
        for key, label in OPERATIONS.items():
            self.operation.addItem(label, key)
        self.size = QComboBox()
        for key, (label, _s) in PRESETS.items():
            self.size.addItem(label, key)
        self.width_spin, self.height_spin = QSpinBox(), QSpinBox()
        for box, limit in ((self.width_spin, 7680), (self.height_spin, 4320)):
            box.setRange(64, limit)
            box.setSingleStep(2)
            box.setKeyboardTracking(False)
        self.mode = QComboBox()
        for key in BATCH_RESIZE_MODES:
            self.mode.addItem(MODES[key], key)
        self.tracking = QComboBox()
        for key in BATCH_TRACKING_MODES:
            self.tracking.addItem(TRACKING_MODES[key].label, key)
        self.color_button = QPushButton("Background colour…")
        self.blur = QSpinBox()
        self.blur.setRange(1, 60)
        self.burn = QCheckBox("Burn in captions (from the project, or a .srt file with the same name)")
        self.template = QLineEdit()
        self.template.setToolTip("Fields: " + ", ".join("{" + k + "}: " + v for k, v in TEMPLATE_FIELDS.items()))
        self.output = OutputSettingsForm()

        what = QGroupBox("Processing")
        f = QFormLayout(what)
        f.addRow("Operation:", self.operation)
        f.addRow("Output size:", self.size)
        row = QHBoxLayout()
        row.addWidget(self.width_spin)
        row.addWidget(QLabel("×"))
        row.addWidget(self.height_spin)
        f.addRow("Custom size:", row)
        f.addRow("Fit mode:", self.mode)
        f.addRow("Tracking mode:", self.tracking)
        f.addRow("Background:", self.color_button)
        f.addRow("Blur strength:", self.blur)
        f.addRow("", self.burn)
        f.addRow("File name:", self.template)
        enc = QGroupBox("Encoding")
        QVBoxLayout(enc).addWidget(self.output)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(what)
        layout.addWidget(enc)
        layout.addWidget(muted("Tracking uses the defaults from Settings → Tracking. Files are named from the "
                               "template; '.mp4' is added."))

        for box in (self.operation, self.size, self.mode, self.tracking):
            box.currentIndexChanged.connect(self._emit)
        for box in (self.width_spin, self.height_spin, self.blur):
            box.valueChanged.connect(self._emit)
        self.burn.toggled.connect(self._emit)
        self.template.editingFinished.connect(self._emit)
        self.color_button.clicked.connect(self._choose_color)
        for child in self.output.findChildren(QComboBox) + self.output.findChildren(QSpinBox):
            signal = getattr(child, "currentIndexChanged", None) or child.valueChanged
            signal.connect(self._emit)
        for child in self.output.findChildren(QCheckBox):
            child.toggled.connect(self._emit)
        self.set_preset(preset or RenderPreset())

    def _choose_color(self) -> None:
        color = QColorDialog.getColor(QColor(self._background), self, "Background colour")
        if color.isValid():
            self._background = color.name()
            self._emit()

    def _emit(self, *_args) -> None:
        self._update_enabled()
        if not self._filling:
            self.changed.emit()

    def set_preset(self, p: RenderPreset) -> None:
        self._filling = True
        try:
            for box, value in ((self.operation, p.operation), (self.size, p.resize.preset),
                               (self.mode, p.resize.mode), (self.tracking, p.tracking_mode)):
                box.setCurrentIndex(max(0, box.findData(value)))
            self.width_spin.setValue(int(p.resize.width))
            self.height_spin.setValue(int(p.resize.height))
            self.blur.setValue(int(p.resize.blur))
            self.burn.setChecked(p.resize.burn_captions)
            self.template.setText(p.filename_template)
            self._background = p.resize.background
            self.output.set_settings(p.output)
        finally:
            self._filling = False
        self._update_enabled()

    def preset(self) -> RenderPreset:
        resize = ResizeSettings(preset=self.size.currentData(), width=self.width_spin.value(),
                                height=self.height_spin.value(), mode=self.mode.currentData(),
                                background=self._background, blur=self.blur.value(),
                                burn_captions=self.burn.isChecked(), use_cuts=False)
        preset = RenderPreset(operation=self.operation.currentData(), resize=resize,
                              tracking_mode=self.tracking.currentData(), output=self.output.settings(),
                              filename_template=self.template.text().strip())
        preset.validate()
        return preset

    def _update_enabled(self) -> None:
        reframe = self.operation.currentData() == "reframe"
        custom = self.size.currentData() == "custom"
        self.width_spin.setEnabled(custom)
        self.height_spin.setEnabled(custom)
        self.mode.setEnabled(not reframe)
        self.tracking.setEnabled(reframe)
        mode = self.mode.currentData()
        self.color_button.setEnabled(not reframe and mode in ("fit", "custom"))
        self.color_button.setStyleSheet(f"background: {self._background};")
        self.blur.setEnabled(not reframe and mode == "blur")


class PresetDialog(QDialog):
    """Edit one video's own settings (override)."""

    def __init__(self, parent: QWidget | None, title: str, preset: RenderPreset):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.editor = PresetEditor(preset)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.editor)
        layout.addWidget(buttons)

    def accept(self) -> None:  # noqa: D102
        try:
            self.result_preset = self.editor.preset()
        except StudioError as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        super().accept()

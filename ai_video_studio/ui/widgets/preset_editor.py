"""Editor for a batch export preset (shared batch settings or one video's override)."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio.captions.style import CaptionStyle
from ai_video_studio.core.errors import StudioError
from ai_video_studio.reframe.model import TRACKING_MODES
from ai_video_studio.render.batch import BATCH_RESIZE_MODES, BATCH_TRACKING_MODES, OPERATIONS, RenderPreset
from ai_video_studio.render.layout import MODES, PRESETS, ResizeSettings
from ai_video_studio.render.naming import TEMPLATE_FIELDS
from ai_video_studio.smartcut.model import SmartCutSettings
from ai_video_studio.ui.widgets.common import muted
from ai_video_studio.ui.widgets.effects_form import EffectsForm
from ai_video_studio.ui.widgets.output_settings import OutputSettingsForm

OWN_STYLE = "__own__"
PRESET_STYLE = "__preset__"


class PresetEditor(QWidget):
    changed = Signal()

    def __init__(self, preset: RenderPreset | None = None, parent: QWidget | None = None, caption_presets=None):
        """``caption_presets``: the caption preset store (``names()``, ``get(name)``) for the caption style list."""
        super().__init__(parent)
        self._filling = False
        self._background = "#000000"
        self._caption_presets = caption_presets
        self._preset_style: CaptionStyle | None = None  # a style stored in the loaded preset
        self._cut_settings = SmartCutSettings()
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
        self.caption_style = QComboBox()
        self.caption_style.setToolTip("Caption template for every video in the batch, or each video's own style "
                                      "(Captions page).")
        self.smart_cut = QCheckBox("Remove silence (Smart Cut)")
        self.smart_cut.setToolTip("Silence is detected automatically in each video. Videos of the open project with "
                                  "cuts applied on the Smart Cut page use those reviewed cuts.")
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(-90, -5)
        self.threshold.setDecimals(0)
        self.threshold.setSuffix(" dB")
        self.min_silence = QDoubleSpinBox()
        self.min_silence.setRange(0.1, 30)
        self.min_silence.setSingleStep(0.1)
        self.min_silence.setDecimals(2)
        self.min_silence.setSuffix(" s")
        self.effects = EffectsForm()
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
        f.addRow("File name:", self.template)
        cut = QGroupBox("Smart Cut")
        cf = QFormLayout(cut)
        cf.addRow("", self.smart_cut)
        cf.addRow("Silence below:", self.threshold)
        cf.addRow("Minimum silence:", self.min_silence)
        cap = QGroupBox("Captions")
        capf = QFormLayout(cap)
        capf.addRow("", self.burn)
        capf.addRow("Caption style:", self.caption_style)
        enc = QGroupBox("Encoding")
        QVBoxLayout(enc).addWidget(self.output)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(what)
        layout.addWidget(cut)
        layout.addWidget(cap)
        layout.addWidget(self.effects)
        layout.addWidget(enc)
        layout.addWidget(muted("Tracking uses the defaults from Settings → Tracking. Files are named from the "
                               "template; '.mp4' is added."))

        for box in (self.operation, self.size, self.mode, self.tracking, self.caption_style):
            box.currentIndexChanged.connect(self._emit)
        for box in (self.width_spin, self.height_spin, self.blur, self.threshold, self.min_silence):
            box.valueChanged.connect(self._emit)
        self.burn.toggled.connect(self._emit)
        self.smart_cut.toggled.connect(self._emit)
        self.effects.changed.connect(self._emit)
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
            self.smart_cut.setChecked(p.smart_cut)
            self._cut_settings = SmartCutSettings.from_dict(p.smart_cut_settings.to_dict())
            self.threshold.setValue(self._cut_settings.threshold_db)
            self.min_silence.setValue(self._cut_settings.min_silence)
            self.effects.set_effects(p.effects)
            self._preset_style = p.caption_style
            self.caption_style.clear()  # select the preset's own choice
            self.fill_caption_styles()
        finally:
            self._filling = False
        self._update_enabled()

    def fill_caption_styles(self) -> None:
        """List "each video's own style", the style stored in the preset (if any) and the caption presets."""
        current = self.caption_style.currentData() or (PRESET_STYLE if self._preset_style else OWN_STYLE)
        blocked = self.caption_style.blockSignals(True)
        self.caption_style.clear()
        self.caption_style.addItem("Each video's own style (Captions page)", OWN_STYLE)
        if self._preset_style is not None:
            self.caption_style.addItem("Style saved in this preset", PRESET_STYLE)
        for name, builtin in (self._caption_presets.names() if self._caption_presets else []):
            self.caption_style.addItem(f"{name} (built-in)" if builtin else name, name)
        self.caption_style.setCurrentIndex(max(0, self.caption_style.findData(current)))
        self.caption_style.blockSignals(blocked)

    def selected_caption_style(self) -> CaptionStyle | None:
        key = self.caption_style.currentData()
        if key in (None, OWN_STYLE):
            return None
        if key == PRESET_STYLE:
            return self._preset_style
        return self._caption_presets.get(key)

    def preset(self) -> RenderPreset:
        cut_settings = SmartCutSettings.from_dict(self._cut_settings.to_dict())
        cut_settings.threshold_db = self.threshold.value()
        cut_settings.min_silence = self.min_silence.value()
        resize = ResizeSettings(preset=self.size.currentData(), width=self.width_spin.value(),
                                height=self.height_spin.value(), mode=self.mode.currentData(),
                                background=self._background, blur=self.blur.value(),
                                burn_captions=self.burn.isChecked(), use_cuts=False)
        preset = RenderPreset(operation=self.operation.currentData(), resize=resize,
                              tracking_mode=self.tracking.currentData(), output=self.output.settings(),
                              filename_template=self.template.text().strip(), smart_cut=self.smart_cut.isChecked(),
                              smart_cut_settings=cut_settings, caption_style=self.selected_caption_style(),
                              effects=self.effects.effects())
        preset.validate()
        return preset

    def _update_enabled(self) -> None:
        reframe = self.operation.currentData() == "reframe"
        original = self.operation.currentData() == "none"
        custom = self.size.currentData() == "custom"
        self.size.setEnabled(not original)
        self.width_spin.setEnabled(custom and not original)
        self.height_spin.setEnabled(custom and not original)
        self.mode.setEnabled(not reframe and not original)
        for w in (self.threshold, self.min_silence):
            w.setEnabled(self.smart_cut.isChecked())
        self.caption_style.setEnabled(self.burn.isChecked())
        self.tracking.setEnabled(reframe)
        mode = self.mode.currentData()
        self.color_button.setEnabled(not reframe and not original and mode in ("fit", "custom"))
        self.color_button.setStyleSheet(f"background: {self._background};")
        self.blur.setEnabled(not reframe and not original and mode == "blur")


class PresetDialog(QDialog):
    """Edit one video's own settings (override)."""

    def __init__(self, parent: QWidget | None, title: str, preset: RenderPreset, caption_presets=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.editor = PresetEditor(preset, caption_presets=caption_presets)
        scroll = QScrollArea()
        scroll.setWidget(self.editor)
        scroll.setWidgetResizable(True)
        scroll.setMinimumSize(520, 560)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(scroll)
        layout.addWidget(buttons)

    def accept(self) -> None:  # noqa: D102
        try:
            self.result_preset = self.editor.preset()
        except StudioError as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        super().accept()

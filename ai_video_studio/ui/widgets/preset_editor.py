"""Batch edit settings: one card per operation with its own on/off switch.

The cards edit a :class:`RenderPreset` (the shared batch settings or one video's own settings):
Smart Cut, Auto Reframe, Captions, Caption Animation, Resize / Crop, Speed, Audio, Color Grading
and Video Filters. A switched-off card is collapsed. Auto Reframe and Resize /
Crop both decide the framing, so switching one on switches the other off; with both off the
original frame is kept. Encoding and file-name widgets (``output``, ``template``) belong to the
preset too, but the Bulk Processing page shows them in its Output Settings panel.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio.captions.animation import CaptionAnimation
from ai_video_studio.captions.style import CaptionStyle
from ai_video_studio.core.errors import StudioError
from ai_video_studio.reframe.model import TRACKING_MODES
from ai_video_studio.render.batch import BATCH_RESIZE_MODES, BATCH_TRACKING_MODES, RenderPreset, reframe_aspect
from ai_video_studio.render.color import ColorSettings
from ai_video_studio.render.layout import MODES, PRESETS, ResizeSettings
from ai_video_studio.render.naming import TEMPLATE_FIELDS
from ai_video_studio.smartcut.model import SmartCutSettings
from ai_video_studio.ui.widgets.caption_animation import AnimationEditor
from ai_video_studio.ui.widgets.caption_style_editor import CaptionStyleEditor
from ai_video_studio.ui.widgets.cards import ToolCard
from ai_video_studio.ui.widgets.color_grading import ColorGradeForm, FilterPicker
from ai_video_studio.ui.widgets.common import muted
from ai_video_studio.ui.widgets.effects_form import EffectsForm
from ai_video_studio.ui.widgets.output_settings import OutputSettingsForm

OWN_STYLE = "__own__"
PRESET_STYLE = "__preset__"
REFRAME_SIZES = [k for k, (_label, size) in PRESETS.items() if size and reframe_aspect(*size)]


def _slider() -> QSlider:
    s = QSlider(Qt.Orientation.Horizontal)
    s.setRange(0, 100)
    return s


class CaptionStyleDialog(QDialog):
    """Font, size, colours, outline, shadow, box and position for the batch's captions."""

    def __init__(self, parent: QWidget | None, style: CaptionStyle):
        super().__init__(parent)
        self.setWindowTitle("Caption Style for the Batch")
        self.editor = CaptionStyleEditor()
        self.editor.set_style(style)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.editor)
        layout.addWidget(buttons)


class PresetEditor(QWidget):
    changed = Signal()

    def __init__(self, preset: RenderPreset | None = None, parent: QWidget | None = None, caption_presets=None,
                 color_presets=None):
        """``caption_presets``: the caption preset store (``names()``, ``get(name)``) for the caption style list;
        ``color_presets``: the user's colour looks (``render.color.ColorPresetStore``)."""
        super().__init__(parent)
        self._filling = False
        self._background = "#000000"
        self._caption_presets = caption_presets
        self._preset_style: CaptionStyle | None = None  # a style stored in the preset or edited here
        self._cut_settings = SmartCutSettings()
        self._resize = ResizeSettings()

        # -- Smart Cut --
        self.smart_card = ToolCard("Smart Cut", "Remove silent parts automatically", "#2fbf71")
        self.smart_cut = self.smart_card.toggle
        self.smart_cut.setToolTip("Silence is detected in each video during its export. Videos of the open project "
                                  "with cuts applied on the Smart Cut page use those reviewed cuts.")
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(-90, -5)
        self.threshold.setDecimals(0)
        self.threshold.setSuffix(" dB")
        self.min_silence = QDoubleSpinBox()
        self.min_silence.setRange(0.1, 30)
        self.min_silence.setSingleStep(0.1)
        self.min_silence.setDecimals(2)
        self.min_silence.setSuffix(" s")
        self.min_keep = QDoubleSpinBox()
        self.min_keep.setRange(0, 10)
        self.min_keep.setSingleStep(0.1)
        self.min_keep.setDecimals(2)
        self.min_keep.setSuffix(" s")
        self.min_keep.setToolTip("Sounds shorter than this between two silences are cut with them.")
        f = self.smart_card.form
        f.addRow("Silence below:", self.threshold)
        f.addRow("Minimum silence:", self.min_silence)
        f.addRow("Keep at least:", self.min_keep)

        # -- Auto Reframe --
        self.reframe_card = ToolCard("Auto Reframe", "Follow faces and people to frame each video", "#4c8dff")
        self.tracking = QComboBox()
        for key in BATCH_TRACKING_MODES:
            self.tracking.addItem(TRACKING_MODES[key].label, key)
        self.reframe_size = QComboBox()
        for key in REFRAME_SIZES:
            self.reframe_size.addItem(PRESETS[key][0], key)
        f = self.reframe_card.form
        f.addRow("Tracking mode:", self.tracking)
        f.addRow("Output size:", self.reframe_size)
        f.addRow(muted("Each video is analysed during its export with the tracking defaults from Settings."))

        # -- Captions --
        self.caption_card = ToolCard("Captions", "Burn styled captions into every video", "#a970ff")
        self.burn = self.caption_card.toggle
        self.burn.setToolTip("Captions come from the project or a .srt next to the video, or are generated "
                             "automatically (below).")
        self.auto_captions = QCheckBox("Generate captions automatically (Whisper)")
        self.auto_captions.setToolTip("Each video is transcribed in its own export job with the model, language and "
                                      "device from Settings, like Generate Captions on the Captions page. No project "
                                      "captions or .srt files are needed.")
        self.caption_style = QComboBox()
        self.caption_style.setToolTip("Caption style for every video in the batch, or each video's own style "
                                      "(Captions page).")
        self.style_button = QPushButton("More Caption Settings…")
        self.style_info = muted("")
        f = self.caption_card.form
        f.addRow("", self.auto_captions)
        f.addRow("Style:", self.caption_style)
        f.addRow(self.style_info)
        f.addRow(self.style_button)

        # -- Resize / Crop --
        self.resize_card = ToolCard("Resize / Crop", "Set the resolution and framing of every video", "#ff9f43")
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
            self.mode.addItem(MODES[key].split(" —")[0], key)
        self.pos_x, self.pos_y = _slider(), _slider()
        self.zoom = QSpinBox()
        self.zoom.setRange(10, 400)
        self.zoom.setSuffix(" %")
        self.blur = QSpinBox()
        self.blur.setRange(1, 60)
        self.color_button = QPushButton("Background colour…")
        f = self.resize_card.form
        f.addRow("Output size:", self.size)
        dims = QHBoxLayout()
        dims.addWidget(self.width_spin)
        dims.addWidget(QLabel("×"))
        dims.addWidget(self.height_spin)
        f.addRow("Custom size:", dims)
        f.addRow("Mode:", self.mode)
        f.addRow("Position X:", self.pos_x)
        f.addRow("Position Y:", self.pos_y)
        f.addRow("Zoom:", self.zoom)
        f.addRow("Blur strength:", self.blur)
        f.addRow("Background:", self.color_button)

        # -- Speed and Audio (the existing speed/audio form, one group per card) --
        self.effects = EffectsForm()
        self.speed_card = ToolCard("Speed", "Change the playback speed", "#ff5a6e")
        self.audio_card = ToolCard("Audio Processing", "Clean up and enhance the sound", "#e05bd0")
        for card, group, box in ((self.speed_card, self.effects.speed_group, self.effects.speed_enabled),
                                 (self.audio_card, self.effects.audio_group, self.effects.audio_enabled)):
            group.setTitle("")
            group.setFlat(True)
            form = group.layout()
            if isinstance(form, QFormLayout):
                form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            box.hide()  # the card's switch is the on/off control
            card.form.addRow(group)
            card.toggle.toggled.connect(box.setChecked)

        # -- Caption animation (needs captions) --
        self.anim_card = ToolCard("Caption Animation", "Animate every caption (entrance, exit, loop, words)",
                                  "#ffd43b")
        self.animation = AnimationEditor()
        self.anim_card.form.addRow(self.animation)
        self.anim_note = muted("")
        self.anim_card.form.addRow(self.anim_note)

        # -- Color grading and video filters --
        self.color_card = ToolCard("Color Grading", "Cinematic looks and manual colour adjustments", "#1fc8b4")
        self.grade = ColorGradeForm(color_presets)
        self.compare_button = QPushButton("Before / After…")
        self.compare_button.setToolTip("Render one frame of the highlighted (or first ticked) video without and with "
                                       "the colour settings, exactly as the export grades it.")
        self.color_card.form.addRow(self.grade)
        self.color_card.form.addRow(self.compare_button)
        self.filter_card = ToolCard("Video Filters", "Filter presets with adjustable intensity", "#7f8cff")
        self.filters = FilterPicker()
        self.filter_card.form.addRow(self.filters)
        self.filter_card.form.addRow(muted("A filter is added on top of the colour grade; together they are rendered "
                                           "as one set of adjustments, so nothing is applied twice."))

        # -- encoding and file name (shown by the page in its Output Settings panel) --
        self.output = OutputSettingsForm()
        self.template = QLineEdit()
        self.template.setToolTip("Fields: " + ", ".join("{" + k + "}: " + v for k, v in TEMPLATE_FIELDS.items()))

        # Let the cards shrink with the window: long combo-box entries are elided, not widened.
        for box in self.findChildren(QComboBox):
            box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            box.setMinimumContentsLength(8)
        for label in self.findChildren(QLabel):
            label.setWordWrap(True)
        # two columns of cards that stack independently (a tall card does not leave a gap beside it)
        columns = QHBoxLayout(self)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(10)
        for cards in ((self.smart_card, self.caption_card, self.resize_card, self.speed_card, self.color_card),
                      (self.reframe_card, self.anim_card, self.audio_card, self.filter_card)):
            column = QVBoxLayout()
            column.setSpacing(10)
            for card in cards:
                column.addWidget(card)
            column.addStretch(1)
            columns.addLayout(column, 1)
        for card in self.cards():
            card.collapse_when_off()

        self.reframe_card.toggle.toggled.connect(lambda on: self._exclusive(on, self.resize_card))
        self.resize_card.toggle.toggled.connect(lambda on: self._exclusive(on, self.reframe_card))
        for card in (self.smart_card, self.reframe_card, self.caption_card, self.resize_card, self.anim_card,
                     self.color_card, self.filter_card):
            card.toggle.toggled.connect(self._emit)
        self.animation.changed.connect(self._emit)
        self.grade.changed.connect(self._emit)
        self.filters.changed.connect(self._emit)
        for box in (self.tracking, self.reframe_size, self.caption_style, self.size, self.mode):
            box.currentIndexChanged.connect(self._emit)
        for box in (self.threshold, self.min_silence, self.min_keep, self.width_spin, self.height_spin, self.zoom,
                    self.blur, self.pos_x, self.pos_y):
            box.valueChanged.connect(self._emit)
        self.auto_captions.toggled.connect(self._emit)
        self.effects.changed.connect(self._emit)
        self.template.editingFinished.connect(self._emit)
        self.color_button.clicked.connect(self._choose_color)
        self.style_button.clicked.connect(self._edit_caption_style)
        for child in self.output.findChildren(QComboBox) + self.output.findChildren(QSpinBox):
            signal = getattr(child, "currentIndexChanged", None) or child.valueChanged
            signal.connect(self._emit)
        for child in self.output.findChildren(QCheckBox):
            child.toggled.connect(self._emit)
        self.set_preset(preset or RenderPreset())

    # ------------------------------------------------------------------ helpers
    def cards(self) -> list[ToolCard]:
        return [self.smart_card, self.reframe_card, self.caption_card, self.anim_card, self.resize_card,
                self.speed_card, self.audio_card, self.color_card, self.filter_card]

    def _exclusive(self, on: bool, other: ToolCard) -> None:
        if on and other.toggle.isChecked():
            other.toggle.setChecked(False)

    def _choose_color(self) -> None:
        color = QColorDialog.getColor(QColor(self._background), self, "Background colour")
        if color.isValid():
            self._background = color.name()
            self._emit()

    def _edit_caption_style(self) -> None:
        dialog = CaptionStyleDialog(self, self.selected_caption_style() or CaptionStyle())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            style = dialog.editor.style()
            style.validate()
        except StudioError as exc:
            QMessageBox.warning(self, "Caption Style", str(exc))
            return
        self._preset_style = style
        self.caption_style.clear()
        self._current_style_key = PRESET_STYLE
        self.fill_caption_styles()
        self._emit()

    def _emit(self, *_args) -> None:
        self._update_enabled()
        if not self._filling:
            self.changed.emit()

    # ------------------------------------------------------------------ preset in/out
    def set_preset(self, p: RenderPreset) -> None:
        self._filling = True
        try:
            self._resize = ResizeSettings.from_dict(p.resize.to_dict())
            self.reframe_card.toggle.setChecked(p.operation == "reframe")
            self.resize_card.toggle.setChecked(p.operation == "resize")
            self.tracking.setCurrentIndex(max(0, self.tracking.findData(p.tracking_mode)))
            index = self.reframe_size.findData(p.resize.preset)
            self.reframe_size.setCurrentIndex(index if index >= 0 else self.reframe_size.findData("1080x1920"))
            self.size.setCurrentIndex(max(0, self.size.findData(p.resize.preset)))
            self.mode.setCurrentIndex(max(0, self.mode.findData(p.resize.mode)))
            self.width_spin.setValue(int(p.resize.width))
            self.height_spin.setValue(int(p.resize.height))
            self.pos_x.setValue(round(p.resize.pos_x * 100))
            self.pos_y.setValue(round(p.resize.pos_y * 100))
            self.zoom.setValue(round(p.resize.zoom * 100))
            self.blur.setValue(int(p.resize.blur))
            self._background = p.resize.background
            self.burn.setChecked(p.resize.burn_captions)
            self.auto_captions.setChecked(p.auto_captions)
            self.template.setText(p.filename_template)
            self.output.set_settings(p.output)
            self.smart_cut.setChecked(p.smart_cut)
            self._cut_settings = SmartCutSettings.from_dict(p.smart_cut_settings.to_dict())
            self.threshold.setValue(self._cut_settings.threshold_db)
            self.min_silence.setValue(self._cut_settings.min_silence)
            self.min_keep.setValue(self._cut_settings.min_keep)
            self.effects.set_effects(p.effects)
            self.speed_card.toggle.setChecked(p.effects.speed_enabled)
            self.audio_card.toggle.setChecked(p.effects.audio_enabled)
            self.anim_card.toggle.setChecked(bool(p.caption_animation))
            self.animation.set_animation(CaptionAnimation.from_dict(p.caption_animation)
                                         if p.caption_animation else None)
            self.color_card.toggle.setChecked(p.color.grading)
            self.grade.set_settings(p.color)
            self.filter_card.toggle.setChecked(p.color.filtering)
            self.filters.set_filter(p.color.filter, p.color.intensity)
            self._preset_style = p.caption_style
            self._current_style_key = PRESET_STYLE if p.caption_style else OWN_STYLE
            self.caption_style.clear()
            self.fill_caption_styles()
        finally:
            self._filling = False
        self._update_enabled()

    def fill_caption_styles(self) -> None:
        """List "each video's own style", the style stored in the preset (if any) and the caption presets."""
        current = self.caption_style.currentData() or getattr(self, "_current_style_key", OWN_STYLE)
        blocked = self.caption_style.blockSignals(True)
        self.caption_style.clear()
        self.caption_style.addItem("Each video's own style (Captions page)", OWN_STYLE)
        if self._preset_style is not None:
            self.caption_style.addItem("Custom style (this batch)", PRESET_STYLE)
        for name, builtin in (self._caption_presets.names() if self._caption_presets else []):
            self.caption_style.addItem(f"{name} (built-in)" if builtin else name, name)
        self.caption_style.setCurrentIndex(max(0, self.caption_style.findData(current)))
        self.caption_style.blockSignals(blocked)
        self._update_style_info()

    def selected_caption_style(self) -> CaptionStyle | None:
        key = self.caption_style.currentData()
        if key in (None, OWN_STYLE):
            return None
        if key == PRESET_STYLE:
            return self._preset_style
        try:
            return self._caption_presets.get(key)
        except StudioError:
            return None

    def _update_style_info(self) -> None:
        style = self.selected_caption_style()
        if style is None:
            self.style_info.setText("Each video keeps the style set on the Captions page (default style otherwise).")
            return
        self.style_info.setText(f"{style.font_family} {style.font_size}, text {style.text_color}, outline "
                                f"{style.outline_width:g}" + (", box" if style.background else ""))

    def preset(self) -> RenderPreset:
        cut_settings = SmartCutSettings.from_dict(self._cut_settings.to_dict())
        cut_settings.threshold_db = self.threshold.value()
        cut_settings.min_silence = self.min_silence.value()
        cut_settings.min_keep = self.min_keep.value()
        reframe = self.reframe_card.toggle.isChecked()
        operation = "reframe" if reframe else ("resize" if self.resize_card.toggle.isChecked() else "none")
        resize = ResizeSettings.from_dict(self._resize.to_dict())
        resize.preset = self.reframe_size.currentData() if reframe else self.size.currentData()
        resize.width, resize.height = self.width_spin.value(), self.height_spin.value()
        resize.mode = self.mode.currentData()
        resize.pos_x, resize.pos_y = self.pos_x.value() / 100, self.pos_y.value() / 100
        resize.zoom = self.zoom.value() / 100
        resize.blur = self.blur.value()
        resize.background = self._background
        resize.burn_captions = self.burn.isChecked()
        resize.use_cuts = False
        effects = self.effects.effects()
        effects.speed_enabled = self.speed_card.toggle.isChecked()
        effects.audio_enabled = self.audio_card.toggle.isChecked()
        color = ColorSettings(grading=self.color_card.toggle.isChecked(), filter=self.filters.selected(),
                              intensity=self.filters.intensity.value(), filtering=self.filter_card.toggle.isChecked())
        self.grade.apply_to(color)
        animation = None
        if self.anim_card.toggle.isChecked():
            anim = self.animation.animation()
            anim.validate()
            animation = anim.to_dict() if anim.active else None
        preset = RenderPreset(operation=operation, resize=resize, tracking_mode=self.tracking.currentData(),
                              output=self.output.settings(), filename_template=self.template.text().strip(),
                              smart_cut=self.smart_cut.isChecked(), smart_cut_settings=cut_settings,
                              caption_style=self.selected_caption_style(),
                              auto_captions=self.auto_captions.isChecked(), effects=effects, color=color,
                              caption_animation=animation)
        preset.validate()
        return preset

    def enabled_operations(self) -> list[str]:
        """Names of the switched-on operations (for the page's summary)."""
        cards = ((self.smart_card, "Smart Cut"), (self.reframe_card, "Auto Reframe"), (self.caption_card, "Captions"),
                 (self.anim_card, "Caption Animation"), (self.resize_card, "Resize / Crop"), (self.speed_card, "Speed"),
                 (self.audio_card, "Audio"), (self.color_card, "Color Grading"), (self.filter_card, "Video Filters"))
        return [name for card, name in cards if card.toggle.isChecked()]

    def _update_enabled(self) -> None:
        custom = self.size.currentData() == "custom"
        self.width_spin.setEnabled(custom)
        self.height_spin.setEnabled(custom)
        mode = self.mode.currentData()
        self.pos_x.setEnabled(mode in ("fill", "custom"))
        self.pos_y.setEnabled(mode in ("fill", "custom"))
        self.zoom.setEnabled(mode == "custom")
        self.blur.setEnabled(mode == "blur")
        self.color_button.setEnabled(mode in ("fit", "custom"))
        self.color_button.setStyleSheet(f"background: {self._background};")
        self.auto_captions.setEnabled(self.burn.isChecked())
        self.caption_style.setEnabled(self.burn.isChecked())
        self.anim_note.setText("" if self.burn.isChecked() else
                               "⚠ Captions are switched off, so there is nothing to animate.")
        self._update_style_info()


class PresetDialog(QDialog):
    """Edit one video's own settings (override)."""

    def __init__(self, parent: QWidget | None, title: str, preset: RenderPreset, caption_presets=None,
                 color_presets=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.editor = PresetEditor(preset, caption_presets=caption_presets, color_presets=color_presets)
        inner = QWidget()
        box = QVBoxLayout(inner)
        box.addWidget(self.editor)
        out = QFormLayout()
        out.addRow("File name:", self.editor.template)
        box.addLayout(out)
        box.addWidget(self.editor.output)
        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setMinimumSize(760, 600)
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

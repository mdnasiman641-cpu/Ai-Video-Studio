"""Caption animation settings (entrance, exit, loop, word effect, timing), used by the Edit page's
Captions tool and by Bulk Processing. Every value is a field of ``captions.animation.CaptionAnimation``,
which the export renders with libass; nothing here is only visual."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QFormLayout, QHBoxLayout, QLabel, QSlider, QSpinBox, QWidget

from ai_video_studio.captions.animation import (
    ENTRANCES,
    EXITS,
    LOOPS,
    SCALE_LOOPS,
    TEXT_ENTRANCES,
    WORD_EFFECTS,
    AnimationError,
    CaptionAnimation,
)
from ai_video_studio.ui.widgets.common import muted


def _combo(options: dict[str, str]) -> QComboBox:
    combo = QComboBox()
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    for key, label in options.items():
        combo.addItem(label, key)
    return combo


def _ms_spin(maximum: int, value: int) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(0, maximum)
    spin.setSingleStep(50)
    spin.setSuffix(" ms")
    spin.setValue(value)
    return spin


class AnimationEditor(QWidget):
    changed = Signal(object)  # CaptionAnimation (valid)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._filling = False
        self.entrance = _combo(ENTRANCES)
        self.exit = _combo(EXITS)
        self.loop = _combo(LOOPS)
        self.word = _combo(WORD_EFFECTS)
        defaults = CaptionAnimation()
        self.in_ms = _ms_spin(3000, defaults.in_ms)
        self.out_ms = _ms_spin(3000, defaults.out_ms)
        self.delay_ms = _ms_spin(3000, defaults.delay_ms)
        self.period_ms = _ms_spin(5000, defaults.period_ms)
        self.period_ms.setMinimum(200)
        self.intensity = QSlider(Qt.Orientation.Horizontal)
        self.intensity.setRange(0, 100)
        self.intensity.setValue(defaults.intensity)
        self.intensity_label = QLabel(f"{defaults.intensity}%")
        self.intensity_label.setMinimumWidth(40)
        intensity = QHBoxLayout()
        intensity.addWidget(self.intensity, 1)
        intensity.addWidget(self.intensity_label)
        self.summary = muted("")
        self.summary.setWordWrap(True)
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.addRow("Entrance:", self.entrance)
        form.addRow("Entrance duration:", self.in_ms)
        form.addRow("Exit:", self.exit)
        form.addRow("Exit duration:", self.out_ms)
        form.addRow("Loop / emphasis:", self.loop)
        form.addRow("Loop period:", self.period_ms)
        form.addRow("Word effect:", self.word)
        form.addRow("Delay:", self.delay_ms)
        form.addRow("Intensity:", intensity)
        form.addRow(self.summary)
        for combo in (self.entrance, self.exit, self.loop, self.word):
            combo.currentIndexChanged.connect(self._emit)
        for spin in (self.in_ms, self.out_ms, self.delay_ms, self.period_ms):
            spin.valueChanged.connect(self._emit)
        self.intensity.valueChanged.connect(self._emit)
        self._update()

    def animation(self) -> CaptionAnimation:
        return CaptionAnimation(entrance=self.entrance.currentData(), exit=self.exit.currentData(),
                                loop=self.loop.currentData(), word=self.word.currentData(), in_ms=self.in_ms.value(),
                                out_ms=self.out_ms.value(), delay_ms=self.delay_ms.value(),
                                intensity=self.intensity.value(), period_ms=self.period_ms.value())

    def set_animation(self, animation: CaptionAnimation | None) -> None:
        a = animation or CaptionAnimation()
        self._filling = True
        try:
            for combo, value in ((self.entrance, a.entrance), (self.exit, a.exit), (self.loop, a.loop),
                                 (self.word, a.word)):
                combo.setCurrentIndex(max(0, combo.findData(value)))
            self.in_ms.setValue(a.in_ms)
            self.out_ms.setValue(a.out_ms)
            self.delay_ms.setValue(a.delay_ms)
            self.period_ms.setValue(a.period_ms)
            self.intensity.setValue(a.intensity)
        finally:
            self._filling = False
        self._update()

    def _update(self) -> CaptionAnimation | None:
        a = self.animation()
        self.intensity_label.setText(f"{a.intensity}%")
        self.in_ms.setEnabled(a.entrance != "none")
        self.out_ms.setEnabled(a.exit != "none")
        self.period_ms.setEnabled(a.loop != "none")
        # choices that cannot be combined are disabled rather than failing later
        for index in range(self.word.count()):
            key = self.word.itemData(index)
            blocked = key != "none" and (a.entrance in TEXT_ENTRANCES or key == "word_pop" and a.loop in SCALE_LOOPS)
            self.word.model().item(index).setEnabled(not blocked)
        try:
            a.validate()
        except AnimationError as exc:
            self.summary.setText(f"⚠ {exc}")
            return None
        self.summary.setText(a.summary() + (". Word effects follow Whisper word times when the caption has them; "
                                            "otherwise words are spread over the caption." if a.word != "none" else ""))
        return a

    def _emit(self, *_args) -> None:
        animation = self._update()
        if not self._filling and animation is not None:
            self.changed.emit(animation)

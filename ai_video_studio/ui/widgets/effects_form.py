"""Speed and audio effects form (Edit page and Bulk Processing settings)."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio.render.effects import (
    MAX_SPEED,
    MIN_SPEED,
    NOISE_REDUCTION,
    SPEED_CHOICES,
    VOICE_EFFECTS,
    EditEffects,
)


def _speed_box() -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(MIN_SPEED, MAX_SPEED)
    box.setSingleStep(0.25)
    box.setDecimals(2)
    box.setSuffix("×")
    box.setKeyboardTracking(False)
    box.setToolTip("Common values: " + ", ".join(f"{s:g}×" for s in SPEED_CHOICES))
    return box


class EffectsForm(QWidget):
    changed = Signal()

    def __init__(self, effects: EditEffects | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self._filling = False
        self.speed_enabled = QCheckBox("Enable speed change")
        self.video_speed = _speed_box()
        self.link_audio = QCheckBox("Audio follows the video speed (stays in sync)")
        self.audio_speed = _speed_box()
        self.audio_speed.setToolTip("Unlinked audio is changed by its own speed and then cut, or padded with "
                                    "silence, to the video length. It is no longer in sync with the picture.")
        self.preserve_pitch = QCheckBox("Preserve pitch")
        self.audio_enabled = QCheckBox("Enable audio processing")
        self.volume = QSpinBox()
        self.volume.setRange(0, 400)
        self.volume.setSingleStep(10)
        self.volume.setSuffix(" %")
        self.noise = QComboBox()
        for key, label in NOISE_REDUCTION.items():
            self.noise.addItem(label, key)
        self.noise.setToolTip("FFmpeg afftdn (FFT noise reduction) for steady background noise such as hiss or hum.")
        self.voice = QComboBox()
        for key, label in VOICE_EFFECTS.items():
            self.voice.addItem(label, key)
        self.bass, self.treble = QDoubleSpinBox(), QDoubleSpinBox()
        for box in (self.bass, self.treble):
            box.setRange(-12, 12)
            box.setSingleStep(1)
            box.setDecimals(0)
            box.setSuffix(" dB")
        self.fade_in, self.fade_out = QDoubleSpinBox(), QDoubleSpinBox()
        for box in (self.fade_in, self.fade_out):
            box.setRange(0, 10)
            box.setSingleStep(0.5)
            box.setDecimals(1)
            box.setSuffix(" s")
        self.normalize = QCheckBox("Normalise loudness (EBU R128, -16 LUFS)")

        self.speed_group = speed = QGroupBox("Speed")
        sf = QFormLayout(speed)
        sf.addRow("", self.speed_enabled)
        sf.addRow("Video speed:", self.video_speed)
        sf.addRow("", self.link_audio)
        sf.addRow("Audio speed:", self.audio_speed)
        sf.addRow("", self.preserve_pitch)
        self.audio_group = audio = QGroupBox("Audio")
        af = QFormLayout(audio)
        af.addRow("", self.audio_enabled)
        af.addRow("Volume:", self.volume)
        af.addRow("Noise reduction:", self.noise)
        af.addRow("Voice effect:", self.voice)
        af.addRow("Bass:", self.bass)
        af.addRow("Treble:", self.treble)
        af.addRow("Fade in:", self.fade_in)
        af.addRow("Fade out:", self.fade_out)
        af.addRow("", self.normalize)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(speed)
        layout.addWidget(audio)

        for box in (self.speed_enabled, self.link_audio, self.preserve_pitch, self.audio_enabled, self.normalize):
            box.toggled.connect(self._emit)
        for box in (self.video_speed, self.audio_speed, self.volume, self.bass, self.treble, self.fade_in,
                    self.fade_out):
            box.valueChanged.connect(self._emit)
        for box in (self.noise, self.voice):
            box.currentIndexChanged.connect(self._emit)
        self.set_effects(effects or EditEffects())

    def _emit(self, *_args) -> None:
        self._update_enabled()
        if not self._filling:
            self.changed.emit()

    def set_effects(self, e: EditEffects) -> None:
        self._filling = True
        try:
            self.speed_enabled.setChecked(e.speed_enabled)
            self.video_speed.setValue(float(e.video_speed))
            self.link_audio.setChecked(e.link_audio)
            self.audio_speed.setValue(float(e.audio_speed))
            self.preserve_pitch.setChecked(e.preserve_pitch)
            self.audio_enabled.setChecked(e.audio_enabled)
            self.volume.setValue(int(e.volume))
            self.noise.setCurrentIndex(max(0, self.noise.findData(e.noise_reduction)))
            self.voice.setCurrentIndex(max(0, self.voice.findData(e.voice)))
            self.bass.setValue(float(e.bass_db))
            self.treble.setValue(float(e.treble_db))
            self.fade_in.setValue(float(e.fade_in))
            self.fade_out.setValue(float(e.fade_out))
            self.normalize.setChecked(e.normalize)
        finally:
            self._filling = False
        self._update_enabled()

    def effects(self) -> EditEffects:
        e = EditEffects(
            speed_enabled=self.speed_enabled.isChecked(), video_speed=round(self.video_speed.value(), 2),
            link_audio=self.link_audio.isChecked(), audio_speed=round(self.audio_speed.value(), 2),
            preserve_pitch=self.preserve_pitch.isChecked(), audio_enabled=self.audio_enabled.isChecked(),
            volume=self.volume.value(), noise_reduction=self.noise.currentData(), fade_in=self.fade_in.value(),
            fade_out=self.fade_out.value(), normalize=self.normalize.isChecked(), bass_db=self.bass.value(),
            treble_db=self.treble.value(), voice=self.voice.currentData())
        e.validate()
        return e

    def _update_enabled(self) -> None:
        speed = self.speed_enabled.isChecked()
        for w in (self.video_speed, self.link_audio, self.preserve_pitch):
            w.setEnabled(speed)
        self.audio_speed.setEnabled(speed and not self.link_audio.isChecked())
        audio = self.audio_enabled.isChecked()
        for w in (self.volume, self.noise, self.voice, self.bass, self.treble, self.fade_in, self.fade_out,
                  self.normalize):
            w.setEnabled(audio)

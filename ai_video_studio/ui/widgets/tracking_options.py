"""Tracking mode selector and its options, shared by the Settings page (defaults for new
videos) and the Auto Reframe page (the current video). Only the options the selected mode
uses are shown."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QFormLayout, QLabel, QWidget

from ai_video_studio.reframe.model import TRACKING_MODES

FALLBACK_LABELS = {
    "center": "Return to the centre",
    "hold": "Hold the last framing",
    "group": "Frame everyone visible",
}


def _spin(low: float, high: float, step: float, decimals: int, suffix: str, tip: str) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(low, high)
    box.setSingleStep(step)
    box.setDecimals(decimals)
    box.setSuffix(suffix)
    box.setKeyboardTracking(False)
    box.setToolTip(tip)
    return box


class TrackingOptions(QWidget):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._loading = False
        self.mode = QComboBox()
        for key, info in TRACKING_MODES.items():
            self.mode.addItem(info.label, key)
        self.description = QLabel()
        self.description.setObjectName("Muted")
        self.description.setWordWrap(True)
        self.fallback = QComboBox()
        for key, label in FALLBACK_LABELS.items():
            self.fallback.addItem(label, key)
        self.fallback.setToolTip("What the crop does when nobody can be followed (subject lost for longer than the "
                                 "hold time, or no identifiable speaker)")
        self.spins: dict[str, tuple[QDoubleSpinBox, float]] = {
            "confidence_threshold": (_spin(0.30, 0.99, 0.05, 2, "", "Detections with a lower score are ignored. "
                                           "Lowering it below the value used for analysis needs a new analysis."), 1),
            "speech_threshold": (_spin(0.10, 0.95, 0.05, 2, "", "Voice-activity probability counted as speech"), 1),
            "switch_seconds": (_spin(0.0, 5.0, 0.25, 2, " s", "How long the evidence for a new speaker must last "
                                     "before the camera switches"), 1),
            "speaker_hold_seconds": (_spin(0.0, 15.0, 0.5, 1, " s", "How long the last speaker is kept when speech "
                                           "cannot be matched to a visible face"), 1),
            "hold_seconds": (_spin(0.0, 10.0, 0.5, 1, " s", "How long the framing is held when the subject "
                                   "disappears, before the fallback"), 1),
            "smoothing": (_spin(0, 100, 5, 0, " %", "Removes jitter from the detections (applied without delay)"),
                          100),
            "padding": (_spin(0, 100, 5, 0, " %", "Space kept around the face and upper body, as a percentage "
                              "of the face size"), 100),
            "max_upscale": (_spin(1.0, 4.0, 0.1, 2, "×", "Largest enlargement of the crop to the output size. "
                                  "Zoom is limited to stay within it; a warning is shown when even the full crop "
                                  "exceeds it."), 1),
            "analysis_fps": (_spin(0.5, 30, 1, 1, " frames/s", "Frames analysed per second of video for face "
                                   "tracking"), 1),
            "av_fps": (_spin(5, 30, 1, 0, " frames/s", "Frames analysed per second when mouth movement is "
                             "needed (Active Speaker). Lower is faster but matches speech less reliably."), 1),
            "analysis_size": (_spin(480, 1920, 160, 0, " px", "Longer side of the analysed frames (the export "
                                    "keeps full resolution). 640 px analyses about 1.5× faster than 960 px but "
                                    "finds small or distant faces less reliably; 1280 px is slower and finds them "
                                    "better."), 1),
        }
        labels = {
            "confidence_threshold": "Tracking confidence:", "speech_threshold": "Speech threshold:",
            "switch_seconds": "Speaker switch after:", "speaker_hold_seconds": "Keep last speaker:",
            "hold_seconds": "Hold when lost:", "smoothing": "Camera smoothing:", "padding": "Crop padding:",
            "max_upscale": "Maximum upscale:", "analysis_fps": "Analysis rate:", "av_fps": "Mouth analysis rate:",
            "analysis_size": "Analysis frame size:",
        }
        self.form = QFormLayout(self)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.addRow("Tracking mode:", self.mode)
        self.form.addRow(self.description)
        self._rows: dict[str, QLabel] = {}
        for name, (box, _scale) in self.spins.items():
            label = QLabel(labels[name])
            self._rows[name] = label
            self.form.addRow(label, box)
            box.valueChanged.connect(lambda _v: self._emit())
        self._rows["fallback"] = QLabel("Fallback:")
        self.form.addRow(self._rows["fallback"], self.fallback)
        self.mode.currentIndexChanged.connect(lambda _i: self._on_mode())
        self.fallback.currentIndexChanged.connect(lambda _i: self._emit())
        self._on_mode()

    def _emit(self) -> None:
        if not self._loading:
            self.changed.emit()

    def current_mode(self) -> str:
        return self.mode.currentData()

    def _on_mode(self) -> None:
        mode = self.current_mode()
        info = TRACKING_MODES[mode]
        self.description.setText(info.description)
        tracking = mode != "center"
        visible = {
            "confidence_threshold": tracking,
            "speech_threshold": info.uses_speech,
            "switch_seconds": info.uses_speaker,
            "speaker_hold_seconds": info.uses_speaker,
            "hold_seconds": tracking,
            "smoothing": tracking,
            "padding": tracking,
            "max_upscale": True,
            "analysis_fps": tracking,
            "av_fps": "mouth" in info.requires,
            "analysis_size": tracking,
            "fallback": tracking,
        }
        for name, show in visible.items():
            widget = self.fallback if name == "fallback" else self.spins[name][0]
            widget.setVisible(show)
            self._rows[name].setVisible(show)
        self._emit()

    def set_values(self, values: dict) -> None:
        """``values`` uses ReframeSettings field names."""
        self._loading = True
        try:
            if "tracking_mode" in values:
                self.mode.setCurrentIndex(max(0, self.mode.findData(values["tracking_mode"])))
            if "fallback" in values:
                self.fallback.setCurrentIndex(max(0, self.fallback.findData(values["fallback"])))
            for name, (box, scale) in self.spins.items():
                if name in values:
                    box.setValue(float(values[name]) * scale)
        finally:
            self._loading = False
        self._on_mode_silent()

    def _on_mode_silent(self) -> None:
        self._loading = True
        try:
            self._on_mode()
        finally:
            self._loading = False

    def values(self) -> dict:
        out = {"tracking_mode": self.current_mode(), "fallback": self.fallback.currentData()}
        for name, (box, scale) in self.spins.items():
            out[name] = round(box.value() / scale, 4)
        return out

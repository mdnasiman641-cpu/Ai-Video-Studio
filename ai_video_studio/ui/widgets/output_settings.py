"""Export settings form and the export dialog shared by Smart Cut, Resize & Crop and Bulk Processing."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio.core.errors import StudioError
from ai_video_studio.render.output import (
    AUDIO_BITRATES,
    AUDIO_MODES,
    CODECS,
    ENCODER_PREFERENCES,
    FRAME_RATES,
    QUALITY_LABELS,
    RATE_CONTROLS,
    OutputSettings,
)
from ai_video_studio.ui.widgets.common import muted

REENCODE_NOTE = ("The video is re-encoded: cutting, resizing and burning in captions need new frames "
                 "(copying the original streams could only cut at keyframes). NVENC is used only if a test "
                 "encode works; otherwise the CPU encoder is used and the export says so.")


def _combo(options: dict | tuple) -> QComboBox:
    box = QComboBox()
    items = options.items() if isinstance(options, dict) else ((o, o) for o in options)
    for key, label in items:
        box.addItem(str(label), key)
    return box


class OutputSettingsForm(QWidget):
    def __init__(self, settings: OutputSettings | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.codec = _combo(CODECS)
        self.encoder = _combo(ENCODER_PREFERENCES)
        self.quality = _combo(QUALITY_LABELS)
        self.crf = QSpinBox()
        self.crf.setRange(10, 40)
        self.crf.setToolTip("Constant quality (CRF for x264/x265, CQ for NVENC). Lower = better and larger.")
        self.rate_control = _combo(RATE_CONTROLS)
        self.bitrate = QSpinBox()
        self.bitrate.setRange(500, 100_000)
        self.bitrate.setSingleStep(500)
        self.bitrate.setSuffix(" kbit/s")
        self.fps = _combo({f: ("Original (keep source timing)" if f == "original" else f"{f} fps")
                           for f in FRAME_RATES})
        self.audio = _combo(AUDIO_MODES)
        self.audio_bitrate = _combo({b: f"{b} kbit/s" for b in AUDIO_BITRATES})
        self.all_tracks = QCheckBox("Keep all audio tracks (converted to AAC; not possible with cuts)")

        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Codec:", self.codec)
        form.addRow("Encoder:", self.encoder)
        form.addRow("Quality:", self.quality)
        form.addRow("CRF / CQ:", self.crf)
        form.addRow("Rate control:", self.rate_control)
        form.addRow("Bitrate:", self.bitrate)
        form.addRow("Frame rate:", self.fps)
        form.addRow("Audio:", self.audio)
        form.addRow("Audio bitrate:", self.audio_bitrate)
        form.addRow("", self.all_tracks)
        for box in (self.quality, self.rate_control, self.audio):
            box.currentIndexChanged.connect(self._update_enabled)
        self.set_settings(settings or OutputSettings())

    def set_settings(self, s: OutputSettings) -> None:
        for box, value in ((self.codec, s.codec), (self.encoder, s.encoder), (self.quality, s.quality),
                           (self.rate_control, s.rate_control), (self.fps, s.fps), (self.audio, s.audio),
                           (self.audio_bitrate, int(s.audio_bitrate))):
            index = box.findData(value)
            box.setCurrentIndex(max(0, index))
        self.crf.setValue(int(s.crf))
        self.bitrate.setValue(int(s.bitrate_kbps))
        self.all_tracks.setChecked(bool(s.all_audio_tracks))
        self._update_enabled()

    def settings(self) -> OutputSettings:
        s = OutputSettings(
            codec=self.codec.currentData(), encoder=self.encoder.currentData(), quality=self.quality.currentData(),
            crf=self.crf.value(), rate_control=self.rate_control.currentData(), bitrate_kbps=self.bitrate.value(),
            fps=self.fps.currentData(), audio=self.audio.currentData(),
            audio_bitrate=self.audio_bitrate.currentData(), all_audio_tracks=self.all_tracks.isChecked())
        s.validate()
        return s

    def _update_enabled(self) -> None:
        bitrate = self.rate_control.currentData() == "bitrate"
        self.quality.setEnabled(not bitrate)
        self.crf.setEnabled(not bitrate and self.quality.currentData() == "custom")
        self.bitrate.setEnabled(bitrate)
        has_audio = self.audio.currentData() != "none"
        self.audio_bitrate.setEnabled(has_audio)
        self.all_tracks.setEnabled(has_audio)


class ExportDialog(QDialog):
    """Asks for the output file and export settings. Never accepts the source file as output."""

    def __init__(self, parent: QWidget | None, title: str, source: str, suggested: str, settings: OutputSettings,
                 *, captions_available: bool = False, captions_checked: bool = False, notes: list[str] = ()):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.source = source
        self.overwrite = False
        self.path_edit = QLineEdit(suggested)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.path_edit, 1)
        row.addWidget(browse)
        self.form = OutputSettingsForm(settings)
        self.burn_captions = QCheckBox("Burn in captions")
        self.burn_captions.setEnabled(captions_available)
        self.burn_captions.setChecked(captions_available and captions_checked)
        if not captions_available:
            self.burn_captions.setToolTip("This video has no captions.")
        layout = QVBoxLayout(self)
        layout.addWidget(muted("Output file:"))
        layout.addLayout(row)
        layout.addWidget(self.form)
        layout.addWidget(self.burn_captions)
        for note in notes:
            layout.addWidget(muted(note))
        layout.addWidget(muted(REENCODE_NOTE))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Add to Export Queue")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(560, self.sizeHint().height())

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Output File", self.path_edit.text(), "MP4 video (*.mp4)",
                                              options=QFileDialog.Option.DontConfirmOverwrite)
        if path:
            self.path_edit.setText(path)

    def output_path(self) -> str:
        path = self.path_edit.text().strip()
        if path and Path(path).suffix.lower() != ".mp4":
            path += ".mp4"
        return path

    def accept(self) -> None:  # noqa: D102
        path = self.output_path()
        try:
            self.form.settings()
        except StudioError as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        if not path or not Path(path).parent.is_dir():
            QMessageBox.warning(self, self.windowTitle(), "Choose an output file in an existing folder.")
            return
        if Path(path).resolve() == Path(self.source).resolve():
            QMessageBox.warning(self, self.windowTitle(), "The source video is never overwritten. Choose another name.")
            return
        if Path(path).exists():
            answer = QMessageBox.question(self, self.windowTitle(), f"{Path(path).name} already exists. Replace it?\n\n"
                                          "The existing file is replaced only after the export has finished.",
                                          defaultButton=QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.overwrite = True
        self.path_edit.setText(path)
        super().accept()

"""Settings page."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
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

from ai_video_studio.ai.providers import PROVIDERS
from ai_video_studio.captions.transcriber import LANGUAGES, MODELS
from ai_video_studio.core import paths
from ai_video_studio.core.errors import SettingsError, StudioError
from ai_video_studio.storage import secrets
from ai_video_studio.services import Services
from ai_video_studio.ui.widgets.common import Card, muted, open_folder, page_title, section_title


class SettingsPage(QWidget):
    def __init__(self, services: Services, on_tools_changed=None):
        super().__init__()
        self.services = services
        self._on_tools_changed = on_tools_changed

        # FFmpeg
        self.ffmpeg_dir = QLineEdit()
        self.ffmpeg_dir.setPlaceholderText("Empty = use bundled FFmpeg or PATH")
        browse_ffmpeg = QPushButton("Browse…")
        clear_ffmpeg = QPushButton("Clear")
        ffmpeg_row = QHBoxLayout()
        ffmpeg_row.addWidget(self.ffmpeg_dir, 1)
        ffmpeg_row.addWidget(browse_ffmpeg)
        ffmpeg_row.addWidget(clear_ffmpeg)
        self.ffmpeg_status = QLabel()
        self.ffmpeg_status.setWordWrap(True)
        self.detect_button = QPushButton("Detect FFmpeg")

        tools = Card()
        tools.body.addWidget(section_title("FFmpeg"))
        tools_form = QFormLayout()
        tools_form.addRow("FFmpeg folder:", ffmpeg_row)
        tools.body.addLayout(tools_form)
        tools.body.addWidget(self.ffmpeg_status)
        detect_row = QHBoxLayout()
        detect_row.addWidget(self.detect_button)
        detect_row.addStretch(1)
        tools.body.addLayout(detect_row)

        # General
        self.projects_dir = QLineEdit()
        browse_projects = QPushButton("Browse…")
        projects_row = QHBoxLayout()
        projects_row.addWidget(self.projects_dir, 1)
        projects_row.addWidget(browse_projects)
        self.max_jobs = QSpinBox()
        self.max_jobs.setRange(1, 8)
        self.max_jobs.setToolTip("How many background jobs may run at the same time.")
        self.log_level = QComboBox()
        self.log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.reopen_last = QCheckBox("Reopen the last project on startup")
        self.preview_volume = QSpinBox()
        self.preview_volume.setRange(0, 100)
        self.preview_volume.setSuffix(" %")

        general = Card()
        general.body.addWidget(section_title("General"))
        form = QFormLayout()
        form.addRow("Projects folder:", projects_row)
        form.addRow("Max concurrent jobs:", self.max_jobs)
        form.addRow("Preview volume:", self.preview_volume)
        form.addRow("Log level:", self.log_level)
        form.addRow("", self.reopen_last)
        general.body.addLayout(form)

        # Captions / transcription
        self.whisper_model = QComboBox()
        self.whisper_model.addItems(MODELS)
        self.whisper_model_dir = QLineEdit()
        self.whisper_model_dir.setPlaceholderText("Empty = download the selected model automatically")
        browse_model = QPushButton("Browse…")
        model_row = QHBoxLayout()
        model_row.addWidget(self.whisper_model_dir, 1)
        model_row.addWidget(browse_model)
        self.transcription_device = QComboBox()
        for code, name in (("auto", "Automatic (GPU if CUDA works, else CPU)"), ("cuda", "NVIDIA GPU (CUDA)"),
                           ("cpu", "CPU")):
            self.transcription_device.addItem(name, code)
        self.transcription_language = QComboBox()
        for code, name in LANGUAGES:
            self.transcription_language.addItem(name, code)
        self.caption_max_chars = QSpinBox()
        self.caption_max_chars.setRange(10, 120)
        self.caption_max_seconds = QSpinBox()
        self.caption_max_seconds.setRange(1, 20)
        self.caption_max_seconds.setSuffix(" s")
        captions = Card()
        captions.body.addWidget(section_title("Captions (speech recognition)"))
        cform = QFormLayout()
        cform.addRow("Whisper model:", self.whisper_model)
        cform.addRow("Local model folder:", model_row)
        cform.addRow("Device:", self.transcription_device)
        cform.addRow("Default language:", self.transcription_language)
        cform.addRow("Max characters per caption:", self.caption_max_chars)
        cform.addRow("Max duration per caption:", self.caption_max_seconds)
        captions.body.addLayout(cform)
        captions.body.addWidget(muted(
            "Models download on first use into the data folder (models\\). GPU transcription needs the CUDA 12 "
            "cuBLAS and cuDNN 9 libraries; without them the CPU is used automatically."))

        # Export
        self.video_encoder = QComboBox()
        for code, name in (("auto", "Automatic (NVENC if usable, else CPU)"), ("h264_nvenc", "NVIDIA NVENC H.264"),
                           ("libx264", "CPU x264")):
            self.video_encoder.addItem(name, code)
        self.video_quality = QSpinBox()
        self.video_quality.setRange(10, 40)
        self.video_quality.setToolTip("CRF (x264) / CQ (NVENC). Lower = better quality and larger files. 18–23 is typical.")
        export = Card()
        export.body.addWidget(section_title("Export"))
        eform = QFormLayout()
        eform.addRow("Video encoder:", self.video_encoder)
        eform.addRow("Quality (CRF/CQ):", self.video_quality)
        export.body.addLayout(eform)

        # AI correction
        self.ai_provider = QComboBox()
        self.ai_provider.addItem("Off", "none")
        for code, name in PROVIDERS.items():
            self.ai_provider.addItem(name, code)
        self.gemini_model = QLineEdit()
        self.openai_model = QLineEdit()
        self.ai_timeout = QSpinBox()
        self.ai_timeout.setRange(10, 300)
        self.ai_timeout.setSuffix(" s")
        ai = Card()
        ai.body.addWidget(section_title("AI caption correction (optional)"))
        aform = QFormLayout()
        aform.addRow("Provider:", self.ai_provider)
        aform.addRow("Gemini model:", self.gemini_model)
        aform.addRow("OpenAI model:", self.openai_model)
        aform.addRow("Request timeout:", self.ai_timeout)
        self.key_inputs: dict[str, QLineEdit] = {}
        self.key_status: dict[str, QLabel] = {}
        for code, name in PROVIDERS.items():
            key_edit = QLineEdit()
            key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            key_edit.setPlaceholderText("Paste a new key to store it")
            save_key = QPushButton("Store Key")
            remove_key = QPushButton("Remove Key")
            status = QLabel()
            status.setObjectName("Muted")
            row = QHBoxLayout()
            row.addWidget(key_edit, 1)
            row.addWidget(save_key)
            row.addWidget(remove_key)
            aform.addRow(f"{name} API key:", row)
            aform.addRow("", status)
            save_key.clicked.connect(lambda _c=False, p=code: self.store_key(p))
            remove_key.clicked.connect(lambda _c=False, p=code: self.remove_key(p))
            self.key_inputs[code] = key_edit
            self.key_status[code] = status
        ai.body.addLayout(aform)
        ai.body.addWidget(muted(
            "Only caption text is sent, and only when you click AI Check. Keys are read from the "
            "GEMINI_API_KEY / OPENAI_API_KEY environment variables or stored in the Windows Credential "
            "Manager; they are never written to project files, the database or logs."))

        # Data
        data = Card()
        data.body.addWidget(section_title("Application data"))
        data.body.addWidget(muted(f"Data folder: {paths.data_dir()}"))
        data.body.addWidget(muted(f"Logs: {paths.logs_dir()}"))
        open_data = QPushButton("Open Data Folder")
        open_logs = QPushButton("Open Logs Folder")
        data_row = QHBoxLayout()
        data_row.addWidget(open_data)
        data_row.addWidget(open_logs)
        data_row.addStretch(1)
        data.body.addLayout(data_row)

        self.save_button = QPushButton("Save Settings")
        self.save_button.setObjectName("Primary")
        self.revert_button = QPushButton("Revert")
        self.defaults_button = QPushButton("Restore Defaults")
        buttons = QHBoxLayout()
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.revert_button)
        buttons.addWidget(self.defaults_button)
        buttons.addStretch(1)

        content = QWidget()
        inner = QVBoxLayout(content)
        inner.addWidget(page_title("Settings"))
        inner.addWidget(tools)
        inner.addWidget(general)
        inner.addWidget(captions)
        inner.addWidget(export)
        inner.addWidget(ai)
        inner.addWidget(data)
        inner.addLayout(buttons)
        inner.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

        browse_ffmpeg.clicked.connect(self._browse_ffmpeg)
        clear_ffmpeg.clicked.connect(self.ffmpeg_dir.clear)
        browse_projects.clicked.connect(self._browse_projects)
        browse_model.clicked.connect(self._browse_model)
        self.detect_button.clicked.connect(self.detect_tools)
        open_data.clicked.connect(lambda: open_folder(paths.data_dir()))
        open_logs.clicked.connect(lambda: open_folder(paths.logs_dir()))
        self.save_button.clicked.connect(self.save)
        self.revert_button.clicked.connect(self.load)
        self.defaults_button.clicked.connect(self.restore_defaults)

        self.load()
        self.detect_tools()

    def values(self) -> dict:
        return {
            "ffmpeg_dir": self.ffmpeg_dir.text(),
            "projects_dir": self.projects_dir.text(),
            "max_concurrent_jobs": self.max_jobs.value(),
            "log_level": self.log_level.currentText(),
            "reopen_last_project": self.reopen_last.isChecked(),
            "preview_volume": self.preview_volume.value(),
            "whisper_model": self.whisper_model.currentText(),
            "whisper_model_dir": self.whisper_model_dir.text(),
            "transcription_device": self.transcription_device.currentData(),
            "transcription_language": self.transcription_language.currentData(),
            "caption_max_chars": self.caption_max_chars.value(),
            "caption_max_seconds": self.caption_max_seconds.value(),
            "video_encoder": self.video_encoder.currentData(),
            "video_quality": self.video_quality.value(),
            "ai_correction_provider": self.ai_provider.currentData(),
            "gemini_model": self.gemini_model.text(),
            "openai_model": self.openai_model.text(),
            "ai_timeout_seconds": self.ai_timeout.value(),
        }

    def load(self) -> None:
        s = self.services.settings
        self.ffmpeg_dir.setText(s.get("ffmpeg_dir"))
        self.projects_dir.setText(s.get("projects_dir"))
        self.max_jobs.setValue(s.get("max_concurrent_jobs"))
        self.log_level.setCurrentText(s.get("log_level"))
        self.reopen_last.setChecked(s.get("reopen_last_project"))
        self.preview_volume.setValue(s.get("preview_volume"))
        self.whisper_model.setCurrentText(s.get("whisper_model"))
        self.whisper_model_dir.setText(s.get("whisper_model_dir"))
        self.transcription_device.setCurrentIndex(self.transcription_device.findData(s.get("transcription_device")))
        self.transcription_language.setCurrentIndex(
            max(0, self.transcription_language.findData(s.get("transcription_language"))))
        self.caption_max_chars.setValue(s.get("caption_max_chars"))
        self.caption_max_seconds.setValue(s.get("caption_max_seconds"))
        self.video_encoder.setCurrentIndex(self.video_encoder.findData(s.get("video_encoder")))
        self.video_quality.setValue(s.get("video_quality"))
        self.ai_provider.setCurrentIndex(self.ai_provider.findData(s.get("ai_correction_provider")))
        self.gemini_model.setText(s.get("gemini_model"))
        self.openai_model.setText(s.get("openai_model"))
        self.ai_timeout.setValue(s.get("ai_timeout_seconds"))
        self.refresh_key_status()

    def save(self) -> bool:
        try:
            self.services.settings.update(self.values())
        except SettingsError as exc:
            QMessageBox.warning(self, "Invalid Settings", str(exc))
            return False
        self.detect_tools()
        return True

    def restore_defaults(self) -> None:
        answer = QMessageBox.question(self, "Restore Defaults", "Reset all settings to their default values?")
        if answer == QMessageBox.StandardButton.Yes:
            self.services.settings.reset()
            self.load()
            self.detect_tools()

    def detect_tools(self) -> None:
        status = self.services.tool_status()
        lines = []
        for name, path, version in (("FFmpeg", status.ffmpeg, status.ffmpeg_version),
                                    ("FFprobe", status.ffprobe, status.ffprobe_version)):
            if path:
                lines.append(f"✔ {name}: {path}<br><span style='color:#8b919c'>{version or 'version unknown'}</span>")
            else:
                lines.append(f"<span style='color:#ff6b6b'>✘ {name} not found</span>")
        if not status.ok:
            lines.append("Install FFmpeg (e.g. <code>winget install Gyan.FFmpeg</code>) or choose its <i>bin</i> folder above.")
        self.ffmpeg_status.setText("<br>".join(lines))
        if self._on_tools_changed:
            self._on_tools_changed(status)

    def _browse_ffmpeg(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Folder containing ffmpeg and ffprobe", self.ffmpeg_dir.text())
        if folder:
            self.ffmpeg_dir.setText(folder)

    def refresh_key_status(self) -> None:
        for provider, label in self.key_status.items():
            source = secrets.key_source(provider)
            label.setText(f"Key found in {source}." if source else "No key configured.")

    def store_key(self, provider: str) -> None:
        edit = self.key_inputs[provider]
        try:
            secrets.set_api_key(provider, edit.text())
        except StudioError as exc:
            QMessageBox.warning(self, "API Key", str(exc))
            return
        edit.clear()
        self.refresh_key_status()

    def remove_key(self, provider: str) -> None:
        secrets.delete_api_key(provider)
        self.refresh_key_status()
        if secrets.key_source(provider):
            QMessageBox.information(self, "API Key", "The stored key was removed, but a key is still provided "
                                                     "by an environment variable.")

    def _browse_model(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "faster-whisper (CTranslate2) model folder",
                                                  self.whisper_model_dir.text())
        if folder:
            self.whisper_model_dir.setText(folder)

    def _browse_projects(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Default Projects Folder", self.projects_dir.text())
        if folder:
            self.projects_dir.setText(folder)


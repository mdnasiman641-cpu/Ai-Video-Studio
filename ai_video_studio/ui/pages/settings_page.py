"""Settings page."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio.ai.providers import PROVIDERS
from ai_video_studio.captions.models import WHISPER_MODELS, model_status, remove_model
from ai_video_studio.captions.transcriber import LANGUAGES, MODELS
from ai_video_studio.jobs.caption_jobs import MODEL_DOWNLOAD_JOB
from ai_video_studio.jobs.models import Job, JobState
from ai_video_studio.core import paths
from ai_video_studio.core.errors import SettingsError, StudioError
from ai_video_studio.storage import secrets
from ai_video_studio.storage.settings import REFRAME_DEFAULT_KEYS
from ai_video_studio.services import Services
from ai_video_studio.ui.widgets.common import Card, muted, open_folder, page_title, section_title
from ai_video_studio.ui.widgets.tracking_options import TrackingOptions


class ModelsCard(Card):
    """Downloaded Whisper models: status, size, download (background job) and delete."""

    def __init__(self, services: Services):
        super().__init__()
        self.services = services
        self.body.addWidget(section_title("Speech recognition models"))
        self.body.addWidget(muted(
            f"Models are downloaded from Hugging Face only when you ask, checked against their published "
            f"checksums, and kept in {paths.models_dir() / 'whisper'} (not in the program folder, so updates keep "
            f"them). Once downloaded they work offline."))
        self.table = QTableWidget(len(WHISPER_MODELS), 3)
        self.table.setHorizontalHeaderLabels(["Model", "Size", "Status"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(210)
        self.download_button = QPushButton("Download Selected")
        self.delete_button = QPushButton("Delete Selected")
        open_button = QPushButton("Open Models Folder")
        row = QHBoxLayout()
        for b in (self.download_button, self.delete_button, open_button):
            row.addWidget(b)
        row.addStretch(1)
        self.body.addWidget(self.table)
        self.body.addLayout(row)
        self.download_button.clicked.connect(self.download_selected)
        self.delete_button.clicked.connect(self.delete_selected)
        open_button.clicked.connect(lambda: open_folder(paths.models_dir()))
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    def _active(self) -> dict[str, Job]:
        jobs = self.services.jobs.store.list({JobState.QUEUED, JobState.RUNNING})
        return {j.payload.get("model"): j for j in jobs if j.kind == MODEL_DOWNLOAD_JOB}

    def refresh(self) -> None:
        active = self._active()
        for row, name in enumerate(WHISPER_MODELS):
            status = model_status(name)
            job = active.get(name)
            if job is not None:
                text = f"Downloading… {int(job.progress * 100)}%" if job.state is JobState.RUNNING else "Queued"
            elif status["downloaded"]:
                text = f"Downloaded ({status['path']})"
            else:
                text = "Not downloaded"
            size = f"{status['size_mb']} MB" if status["downloaded"] else f"≈ {status['approx_mb']} MB"
            for col, value in enumerate((name, size, text)):
                self.table.setItem(row, col, QTableWidgetItem(value))
        if active and not self._timer.isActive():
            self._timer.start()
        elif not active and self._timer.isActive():
            self._timer.stop()
        self._update_buttons()

    def selected(self) -> str | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return list(WHISPER_MODELS)[rows[0].row()] if rows else None

    def _update_buttons(self) -> None:
        name = self.selected()
        downloaded = name is not None and model_status(name)["downloaded"]
        busy = name in self._active() if name else False
        self.download_button.setEnabled(name is not None and not downloaded and not busy)
        self.delete_button.setEnabled(downloaded and not busy)

    def download_selected(self) -> Job | None:
        name = self.selected()
        if name is None:
            return None
        spec = WHISPER_MODELS[name]
        answer = QMessageBox.question(self, "Download Speech Model",
                                      f"Download the Whisper model '{name}' (about {spec.approx_mb} MB) from "
                                      f"Hugging Face now?")
        if answer != QMessageBox.StandardButton.Yes:
            return None
        active = self._active().get(name)
        job = active or self.services.jobs.submit(MODEL_DOWNLOAD_JOB,
                                                  f"Download Whisper model: {name} (~{spec.approx_mb} MB)",
                                                  {"model": name})
        self.refresh()
        return job

    def delete_selected(self) -> None:
        name = self.selected()
        if name is None:
            return
        answer = QMessageBox.question(self, "Delete Speech Model",
                                      f"Delete the downloaded Whisper model '{name}'? It can be downloaded again later.")
        if answer == QMessageBox.StandardButton.Yes:
            if not remove_model(name):
                QMessageBox.warning(self, "Delete Speech Model", "Some files could not be deleted (in use?).")
            self.refresh()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh()


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
        self.whisper_model_dir.setPlaceholderText("Empty = use the models downloaded below")
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

        # Speech recognition models
        self.models = ModelsCard(services)

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

        # Tracking (auto reframe defaults)
        self.tracking = TrackingOptions()
        tracking = Card()
        tracking.body.addWidget(section_title("Tracking (Auto Reframe)"))
        tracking.body.addWidget(muted(
            "Defaults for videos that have not been reframed yet. Each video keeps its own choice in the "
            "project, which you can change on the Auto Reframe page at any time."))
        tracking.body.addWidget(self.tracking)

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
        inner.addWidget(self.models)
        inner.addWidget(export)
        inner.addWidget(tracking)
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
        } | {key: self.tracking.values()[field] for key, field in REFRAME_DEFAULT_KEYS.items()}

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
        self.tracking.set_values({field: s.get(key) for key, field in REFRAME_DEFAULT_KEYS.items()})
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


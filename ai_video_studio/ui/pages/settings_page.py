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

from ai_video_studio.core import paths
from ai_video_studio.core.errors import SettingsError
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
        }

    def load(self) -> None:
        s = self.services.settings
        self.ffmpeg_dir.setText(s.get("ffmpeg_dir"))
        self.projects_dir.setText(s.get("projects_dir"))
        self.max_jobs.setValue(s.get("max_concurrent_jobs"))
        self.log_level.setCurrentText(s.get("log_level"))
        self.reopen_last.setChecked(s.get("reopen_last_project"))
        self.preview_volume.setValue(s.get("preview_volume"))

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

    def _browse_projects(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Default Projects Folder", self.projects_dir.text())
        if folder:
            self.projects_dir.setText(folder)


"""Main application window: sidebar navigation, pages, menus, status bar."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QWidget,
)

from ai_video_studio import APP_NAME, __version__
from ai_video_studio.core import paths
from ai_video_studio.core.errors import StudioError
from ai_video_studio.jobs.models import Job, JobState
from ai_video_studio.services import Services, ToolStatus
from ai_video_studio.storage.project import PROJECT_EXTENSION
from ai_video_studio.ui.pages.captions import CaptionsPage
from ai_video_studio.ui.pages.edit import EditPage
from ai_video_studio.ui.pages.home import HomePage
from ai_video_studio.ui.pages.media import MediaPage, video_file_filter
from ai_video_studio.ui.pages.planned import PlannedPage
from ai_video_studio.ui.pages.queue import QueuePage
from ai_video_studio.ui.pages.reframe import ReframePage
from ai_video_studio.ui.pages.resize import ResizePage
from ai_video_studio.ui.pages.settings_page import SettingsPage
from ai_video_studio.ui.pages.smart_cut import SmartCutPage
from ai_video_studio.ui.project_controller import JobBridge, ProjectController
from ai_video_studio.ui.theme import app_icon, glyph_icon
from ai_video_studio.ui.widgets.common import open_folder

log = logging.getLogger(__name__)

NAVIGATION = [
    ("Home", "\u2302"),            # ⌂
    ("Media", "\u25a4"),           # ▤
    ("Edit", "\u270e"),            # ✎
    ("Captions", "\u2261"),        # ≡
    ("Auto Reframe", "\u25ce"),    # ◎ face/person tracking and reframing
    ("Smart Cut", "\u2702"),       # ✂
    ("Resize & Crop", "\u2922"),   # ⤢
    ("Bulk Processing", "\u25a6"), # ▦
    ("Export Queue", "\u21e9"),    # ⇩
    ("AI Models", "\u2726"),       # ✦
    ("Settings", "\u2699"),        # ⚙
]
PROJECT_FILTER = f"AI Video Studio project (*{PROJECT_EXTENSION})"


class MainWindow(QMainWindow):
    def __init__(self, services: Services):
        super().__init__()
        self.services = services
        # Parented to the window so they are destroyed with it, not later in arbitrary order at
        # interpreter exit (which crashed in QObject::~QObject).
        self.bridge = JobBridge(services, parent=self)
        self.controller = ProjectController(services, self.bridge, parent=self)
        self.setWindowIcon(app_icon())
        self.setAcceptDrops(True)
        self.resize(1440, 900)
        self.setMinimumSize(1024, 640)
        self.project_label = QLabel()
        self.jobs_label = QLabel()
        self.ffmpeg_label = QLabel()

        self.sidebar = QListWidget()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setFixedWidth(200)
        self.pages = QStackedWidget()

        self.home_page = HomePage(services)
        self.media_page = MediaPage(self.controller)
        self.edit_page = EditPage(self.controller)
        self.captions_page = CaptionsPage(self.controller, self.bridge)
        self.reframe_page = ReframePage(self.controller, self.bridge)
        self.smart_cut_page = SmartCutPage(self.controller, self.bridge)
        self.resize_page = ResizePage(self.controller, self.bridge)
        self.queue_page = QueuePage(services, self.bridge)
        self.settings_page = SettingsPage(services, on_tools_changed=self._on_tools_changed)
        built = {
            "Home": self.home_page,
            "Media": self.media_page,
            "Edit": self.edit_page,
            "Captions": self.captions_page,
            "Auto Reframe": self.reframe_page,
            "Smart Cut": self.smart_cut_page,
            "Resize & Crop": self.resize_page,
            "Export Queue": self.queue_page,
            "Settings": self.settings_page,
        }
        self._page_index: dict[str, int] = {}
        for name, icon in NAVIGATION:
            page = built.get(name) or PlannedPage(name)
            self._page_index[name] = self.pages.addWidget(page)
            self.sidebar.addItem(QListWidgetItem(glyph_icon(icon), name))

        central = QWidget()
        row = QHBoxLayout(central)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self.sidebar)
        row.addWidget(self.pages, 1)
        self.setCentralWidget(central)

        status = self.statusBar()
        status.addWidget(self.project_label, 1)
        status.addPermanentWidget(self.jobs_label)
        status.addPermanentWidget(self.ffmpeg_label)

        self._build_menu()

        self.sidebar.currentRowChanged.connect(self._on_nav)
        self.home_page.new_requested.connect(self.new_project)
        self.home_page.open_requested.connect(self.open_project_dialog)
        self.home_page.open_path_requested.connect(self.open_project)
        self.home_page.import_requested.connect(self.import_videos)
        self.home_page.settings_requested.connect(lambda: self.navigate("Settings"))
        self.media_page.open_in_editor.connect(self._open_in_editor)
        self.controller.project_changed.connect(self._update_title)
        self.controller.dirty_changed.connect(lambda _d: self._update_title())
        self.bridge.job_changed.connect(self._on_job_changed)

        self.sidebar.setCurrentRow(0)
        self._update_title()
        self._update_jobs_label()

    # -- menu ----------------------------------------------------------------
    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self._add_action(file_menu, "&New Project…", self.new_project, QKeySequence.StandardKey.New)
        self._add_action(file_menu, "&Open Project…", self.open_project_dialog, QKeySequence.StandardKey.Open)
        self._add_action(file_menu, "&Save Project", self.save_project, QKeySequence.StandardKey.Save)
        self._add_action(file_menu, "Save Project &As…", self.save_project_as, QKeySequence("Ctrl+Shift+S"))
        self._add_action(file_menu, "&Close Project", self.close_project)
        file_menu.addSeparator()
        self._add_action(file_menu, "&Import Videos…", self.import_videos, QKeySequence("Ctrl+I"))
        file_menu.addSeparator()
        self._add_action(file_menu, "E&xit", self.close, QKeySequence.StandardKey.Quit)

        help_menu = self.menuBar().addMenu("&Help")
        self._add_action(help_menu, "Open &Logs Folder", lambda: open_folder(paths.logs_dir()))
        self._add_action(help_menu, "&About", self._about)

    def _add_action(self, menu, text, slot, shortcut=None) -> QAction:
        action = QAction(text, self)
        if shortcut is not None:
            action.setShortcut(shortcut)
        action.triggered.connect(slot)
        menu.addAction(action)
        return action

    # -- navigation ----------------------------------------------------------
    def navigate(self, name: str) -> None:
        self.sidebar.setCurrentRow(list(self._page_index).index(name))

    def _on_nav(self, row: int) -> None:
        current = self.pages.currentWidget()
        if current in self.player_pages() and self.pages.widget(row) is not current:
            current.stop_playback()
        self.pages.setCurrentIndex(row)
        if row == self._page_index["Home"]:
            self.home_page.refresh_recent()

    def player_pages(self) -> list:
        """Pages with a video player; playback stops when they are left or the project closes."""
        return [self.edit_page, self.captions_page, self.reframe_page, self.smart_cut_page, self.resize_page]

    def _open_in_editor(self, media_id: str) -> None:
        self.navigate("Edit")
        self.edit_page.show_media(media_id)

    # -- project commands ----------------------------------------------------
    def confirm_discard(self) -> bool:
        """Ask to save unsaved changes. Returns False if the user cancelled."""
        project = self.controller.project
        if project is None or not project.dirty:
            return True
        answer = QMessageBox.question(
            self, "Unsaved Changes", f"Save changes to “{project.name}”?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self.save_project()
        return answer == QMessageBox.StandardButton.Discard

    def new_project(self) -> None:
        if not self.confirm_discard():
            return
        name, ok = QInputDialog.getText(self, "New Project", "Project name:", text="Untitled")
        if not ok:
            return
        self.controller.new_project(name)
        self.navigate("Media")

    def open_project_dialog(self) -> None:
        if not self.confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Project", self.services.settings.get("projects_dir"), PROJECT_FILTER
        )
        if path:
            self.open_project(path, confirmed=True)

    def open_project(self, path: str, confirmed: bool = False) -> bool:
        if not confirmed and not self.confirm_discard():
            return False
        try:
            self.controller.open_project(path)
        except StudioError as exc:
            log.warning("Failed to open project", extra={"path": path, "error": str(exc)})
            QMessageBox.critical(self, "Open Project", str(exc))
            return False
        self.home_page.refresh_recent()
        self.navigate("Media")
        return True

    def save_project(self) -> bool:
        project = self.controller.project
        if project is None:
            return False
        if project.path is None:
            return self.save_project_as()
        return self._save(None)

    def save_project_as(self) -> bool:
        project = self.controller.project
        if project is None:
            return False
        folder = Path(self.services.settings.get("projects_dir") or Path.home())
        suggested = str(project.path or folder / f"{project.name}{PROJECT_EXTENSION}")
        path, _ = QFileDialog.getSaveFileName(self, "Save Project As", suggested, PROJECT_FILTER)
        if not path:
            return False
        return self._save(path)

    def _save(self, path: str | None) -> bool:
        try:
            saved = self.controller.save_project(path)
        except StudioError as exc:
            QMessageBox.critical(self, "Save Project", str(exc))
            return False
        self.statusBar().showMessage(f"Saved {saved}", 5000)
        self.home_page.refresh_recent()
        return True

    def close_project(self) -> None:
        if self.confirm_discard():
            for page in self.player_pages():
                page.stop_playback()
            self.controller.close_project()
            self.navigate("Home")

    def import_videos(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Import Videos", "", video_file_filter())
        if files:
            self.media_page.import_paths(files)
            self.navigate("Media")

    # -- drag and drop anywhere on the window --------------------------------
    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        projects = [p for p in paths if p.lower().endswith(PROJECT_EXTENSION)]
        if len(projects) == 1 and len(paths) == 1:
            self.open_project(projects[0])
        elif paths:
            self.media_page.import_paths(paths)
            self.navigate("Media")
        event.acceptProposedAction()

    # -- status --------------------------------------------------------------
    def _update_title(self) -> None:
        project = self.controller.project
        if project is None:
            self.setWindowTitle(f"{APP_NAME} {__version__}")
            self.project_label.setText("No project open")
            return
        star = "*" if project.dirty else ""
        self.setWindowTitle(f"{project.name}{star} — {APP_NAME}")
        where = str(project.path) if project.path else "not saved yet"
        self.project_label.setText(f"Project: {project.name}{star} ({where}) · {len(project.media)} video(s)")

    def _on_job_changed(self, job: Job) -> None:
        self._update_jobs_label()
        if job.state is JobState.COMPLETED or job.state is JobState.FAILED:
            self._update_title()

    def _update_jobs_label(self) -> None:
        counts = self.services.jobs.store.counts()
        paused = " (paused)" if self.services.jobs.paused else ""
        self.jobs_label.setText(
            f"Jobs: {counts[JobState.RUNNING]} running, {counts[JobState.QUEUED]} queued{paused}"
        )

    def _on_tools_changed(self, status: ToolStatus) -> None:
        self.ffmpeg_label.setText("FFmpeg: OK" if status.ok else "FFmpeg: NOT FOUND")
        self.ffmpeg_label.setStyleSheet("color: #5fd38d;" if status.ok else "color: #ff6b6b;")
        self.home_page.set_tool_status(status)

    def _about(self) -> None:
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<b>{APP_NAME}</b> {__version__}<br>Desktop video editor for Windows.<br><br>"
            f"Data folder: {paths.data_dir()}",
        )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if not self.confirm_discard():
            event.ignore()
            return
        self.edit_page.stop_playback()
        self.captions_page.stop_playback()
        self.reframe_page.shutdown()
        self.smart_cut_page.shutdown()
        self.resize_page.shutdown()
        event.accept()

"""Home page: new/open project, recent projects, environment status."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio import APP_NAME, __version__
from ai_video_studio.services import Services, ToolStatus
from ai_video_studio.ui.widgets.common import Card, muted, page_title, section_title


class HomePage(QWidget):
    new_requested = Signal()
    open_requested = Signal()
    open_path_requested = Signal(str)
    import_requested = Signal()
    settings_requested = Signal()

    def __init__(self, services: Services):
        super().__init__()
        self.services = services

        new_button = QPushButton("New Project")
        new_button.setObjectName("Primary")
        open_button = QPushButton("Open Project…")
        import_button = QPushButton("Import Videos…")
        actions = QHBoxLayout()
        for b in (new_button, open_button, import_button):
            actions.addWidget(b)
        actions.addStretch(1)

        recent_card = Card()
        recent_card.body.addWidget(section_title("Recent projects"))
        self.recent_list = QListWidget()
        recent_card.body.addWidget(self.recent_list, 1)
        self.open_recent_button = QPushButton("Open")
        self.remove_recent_button = QPushButton("Remove from List")
        recent_buttons = QHBoxLayout()
        recent_buttons.addWidget(self.open_recent_button)
        recent_buttons.addWidget(self.remove_recent_button)
        recent_buttons.addStretch(1)
        recent_card.body.addLayout(recent_buttons)

        status_card = Card()
        status_card.body.addWidget(section_title("Environment"))
        self.ffmpeg_label = QLabel("Checking FFmpeg…")
        self.ffmpeg_label.setWordWrap(True)
        status_card.body.addWidget(self.ffmpeg_label)
        settings_button = QPushButton("Open Settings")
        status_card.body.addWidget(settings_button, 0, Qt.AlignmentFlag.AlignLeft)
        status_card.body.addStretch(1)
        status_card.setMaximumWidth(380)

        row = QHBoxLayout()
        row.addWidget(recent_card, 2)
        row.addWidget(status_card, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(page_title(APP_NAME))
        layout.addWidget(muted(f"Version {__version__} · Drag video files anywhere onto the window to import them."))
        layout.addLayout(actions)
        layout.addLayout(row, 1)

        new_button.clicked.connect(self.new_requested)
        open_button.clicked.connect(self.open_requested)
        import_button.clicked.connect(self.import_requested)
        settings_button.clicked.connect(self.settings_requested)
        self.open_recent_button.clicked.connect(self._open_selected)
        self.remove_recent_button.clicked.connect(self._remove_selected)
        self.recent_list.itemDoubleClicked.connect(lambda _i: self._open_selected())
        self.recent_list.itemSelectionChanged.connect(self._update_buttons)
        self.refresh_recent()

    def refresh_recent(self) -> None:
        self.recent_list.clear()
        for entry in self.services.recent.list():
            text = f"{entry['name']}  —  {entry['path']}"
            if not entry["exists"]:
                text += "  (missing)"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, entry["path"])
            if not entry["exists"]:
                item.setForeground(Qt.GlobalColor.gray)
            self.recent_list.addItem(item)
        if self.recent_list.count() == 0:
            placeholder = QListWidgetItem("No recent projects")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.recent_list.addItem(placeholder)
        self._update_buttons()

    def set_tool_status(self, status: ToolStatus) -> None:
        if status.ok:
            self.ffmpeg_label.setObjectName("Ok")
            self.ffmpeg_label.setText(f"✔ FFmpeg found\n{status.ffmpeg_version or status.ffmpeg}")
        else:
            self.ffmpeg_label.setObjectName("Error")
            missing = [n for n, p in (("ffmpeg", status.ffmpeg), ("ffprobe", status.ffprobe)) if p is None]
            self.ffmpeg_label.setText(
                f"✘ Not found: {', '.join(missing)}.\nVideo metadata and export need FFmpeg. "
                "Install it (winget install Gyan.FFmpeg) or set its folder in Settings."
            )
        self.ffmpeg_label.style().unpolish(self.ffmpeg_label)
        self.ffmpeg_label.style().polish(self.ffmpeg_label)

    def _selected_path(self) -> str | None:
        item = self.recent_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item and item.isSelected() else None

    def _open_selected(self) -> None:
        path = self._selected_path()
        if path:
            self.open_path_requested.emit(path)

    def _remove_selected(self) -> None:
        path = self._selected_path()
        if path:
            self.services.recent.remove(path)
            self.refresh_recent()

    def _update_buttons(self) -> None:
        has = self._selected_path() is not None
        self.open_recent_button.setEnabled(has)
        self.remove_recent_button.setEnabled(has)

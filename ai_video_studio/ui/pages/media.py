"""Media page: import (dialog or drag-and-drop), list, metadata, remove."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio.core.media import (
    VIDEO_EXTENSIONS,
    MediaItem,
    MediaStatus,
    format_duration,
    format_size,
)
from ai_video_studio.ui.project_controller import ProjectController
from ai_video_studio.ui.widgets.common import Card, muted, page_title, reveal_in_explorer, section_title

COLUMNS = ["Name", "Status", "Duration", "Resolution", "FPS", "Video", "Audio", "Size"]
STATUS_TEXT = {
    MediaStatus.PENDING: "Reading…",
    MediaStatus.READY: "Ready",
    MediaStatus.ERROR: "Error",
    MediaStatus.MISSING: "Missing",
}


def video_file_filter() -> str:
    patterns = " ".join(f"*{ext}" for ext in sorted(VIDEO_EXTENSIONS))
    return f"Video files ({patterns});;All files (*)"


class MediaTable(QTableWidget):
    files_dropped = Signal(list)

    def __init__(self) -> None:
        super().__init__(0, len(COLUMNS))
        self.setHorizontalHeaderLabels(COLUMNS)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, len(COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):  # noqa: N802
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)
        else:
            super().dropEvent(event)


class MediaPage(QWidget):
    open_in_editor = Signal(str)  # media id

    def __init__(self, controller: ProjectController):
        super().__init__()
        self.controller = controller

        self.import_button = QPushButton("Import Videos…")
        self.import_button.setObjectName("Primary")
        self.import_folder_button = QPushButton("Import Folder…")
        self.remove_button = QPushButton("Remove from Project")
        self.reprobe_button = QPushButton("Re-read Metadata")
        self.reveal_button = QPushButton("Show in Folder")
        self.edit_button = QPushButton("Open in Editor")
        self.summary = muted("No project open. Import videos to start a new project.", wrap=False)

        toolbar = QHBoxLayout()
        for b in (self.import_button, self.import_folder_button, self.remove_button,
                  self.reprobe_button, self.reveal_button, self.edit_button):
            toolbar.addWidget(b)
        toolbar.addStretch(1)

        self.table = MediaTable()
        self.table.setToolTip("Drag and drop video files or folders here")

        details = Card()
        details.body.addWidget(section_title("Details"))
        self.details_form = QFormLayout()
        details.body.addLayout(self.details_form)
        details.body.addStretch(1)
        self.detail_labels: dict[str, QLabel] = {}
        for key in ("File", "Folder", "Status", "Duration", "Resolution", "Frame rate", "Video codec",
                    "Pixel format", "Rotation", "Bit rate", "Container", "Audio", "File size", "Error"):
            label = QLabel("-")
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.detail_labels[key] = label
            self.details_form.addRow(f"{key}:", label)
        details.setMinimumWidth(280)

        splitter = QSplitter()
        splitter.addWidget(self.table)
        splitter.addWidget(details)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(page_title("Media"))
        layout.addLayout(toolbar)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.summary)

        self.import_button.clicked.connect(self.import_files)
        self.import_folder_button.clicked.connect(self.import_folder)
        self.remove_button.clicked.connect(self.remove_selected)
        self.reprobe_button.clicked.connect(self.reprobe_selected)
        self.reveal_button.clicked.connect(self.reveal_selected)
        self.edit_button.clicked.connect(self._open_current_in_editor)
        self.table.files_dropped.connect(self.import_paths)
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.cellDoubleClicked.connect(lambda row, _c: self._open_row_in_editor(row))

        controller.media_list_changed.connect(self.refresh)
        controller.media_updated.connect(self._refresh_item)
        self.refresh()

    # -- actions -------------------------------------------------------------
    def import_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Import Videos", "", video_file_filter())
        if files:
            self.import_paths(files)

    def import_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Import All Videos in Folder")
        if folder:
            self.import_paths([folder])

    def import_paths(self, paths: list[str]) -> None:
        report = self.controller.import_paths(paths)
        self.summary.setText(report.summary())
        if report.rejected:
            lines = "\n".join(f"• {p}: {reason}" for p, reason in report.rejected[:20])
            more = f"\n…and {len(report.rejected) - 20} more" if len(report.rejected) > 20 else ""
            QMessageBox.warning(self, "Some files were skipped", f"{report.summary()}.\n\n{lines}{more}")

    def remove_selected(self) -> None:
        ids = self.selected_ids()
        if not ids:
            return
        answer = QMessageBox.question(
            self, "Remove Videos",
            f"Remove {len(ids)} video(s) from the project?\nThe files on disk are not deleted.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.remove_media(ids)

    def reprobe_selected(self) -> None:
        count = self.controller.reprobe(self.selected_ids())
        self.summary.setText(f"Re-reading metadata for {count} video(s)")

    def reveal_selected(self) -> None:
        item = self._current_item()
        if item:
            reveal_in_explorer(item.path)

    # -- helpers -------------------------------------------------------------
    def selected_ids(self) -> set[str]:
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        return {self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in rows}

    def _current_item(self) -> MediaItem | None:
        project = self.controller.project
        ids = self.selected_ids()
        if project is None or len(ids) != 1:
            return None
        return project.find_media(next(iter(ids)))

    def _open_current_in_editor(self) -> None:
        item = self._current_item()
        if item:
            self.open_in_editor.emit(item.id)

    def _open_row_in_editor(self, row: int) -> None:
        self.open_in_editor.emit(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole))

    def refresh(self) -> None:
        project = self.controller.project
        media = project.media if project else []
        selected = self.selected_ids()
        self.table.setRowCount(len(media))
        for row, item in enumerate(media):
            self._fill_row(row, item)
            if item.id in selected:
                self.table.selectRow(row)
        if project is None:
            self.summary.setText("No project open. Import videos to start a new project.")
        else:
            self.summary.setText(f"{len(media)} video(s) in project “{project.name}”")
        self._on_selection()

    def _refresh_item(self, media_id: str) -> None:
        project = self.controller.project
        if project is None:
            return
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == media_id:
                self._fill_row(row, project.find_media(media_id))
                break
        self._on_selection()

    def _fill_row(self, row: int, item: MediaItem) -> None:
        meta = item.metadata
        status = item.status
        values = [
            item.name,
            STATUS_TEXT[status],
            format_duration(meta.duration) if meta else "-",
            meta.resolution_text if meta else "-",
            f"{meta.fps:g}" if meta else "-",
            meta.video_codec if meta else "-",
            (meta.audio_codec or "-") if meta and meta.has_audio else ("none" if meta else "-"),
            format_size(meta.file_size) if meta else "-",
        ]
        for col, value in enumerate(values):
            cell = QTableWidgetItem(value)
            if col == 0:
                cell.setData(Qt.ItemDataRole.UserRole, item.id)
                cell.setToolTip(item.path)
            if col == 1 and status in (MediaStatus.ERROR, MediaStatus.MISSING):
                cell.setForeground(Qt.GlobalColor.red)
                cell.setToolTip(item.error or "File not found on disk")
            self.table.setItem(row, col, cell)

    def _on_selection(self) -> None:
        ids = self.selected_ids()
        has_project = self.controller.project is not None
        item = self._current_item()
        self.remove_button.setEnabled(bool(ids))
        self.reprobe_button.setEnabled(bool(ids))
        self.reveal_button.setEnabled(item is not None)
        self.edit_button.setEnabled(item is not None and item.status is not MediaStatus.MISSING)
        self.import_folder_button.setEnabled(True)
        self._show_details(item if has_project else None)

    def _show_details(self, item: MediaItem | None) -> None:
        for label in self.detail_labels.values():
            label.setText("-")
        if item is None:
            return
        meta = item.metadata
        d = self.detail_labels
        d["File"].setText(item.name)
        d["Folder"].setText(str(Path(item.path).parent))
        d["Status"].setText(STATUS_TEXT[item.status])
        d["Error"].setText(item.error or "-")
        if meta:
            d["Duration"].setText(format_duration(meta.duration))
            d["Resolution"].setText(
                meta.resolution_text + (f" (stored {meta.width}x{meta.height})" if meta.rotation else "")
            )
            d["Frame rate"].setText(f"{meta.fps:g} fps")
            d["Video codec"].setText(meta.video_codec)
            d["Pixel format"].setText(meta.pixel_format or "-")
            d["Rotation"].setText(f"{meta.rotation}°")
            d["Bit rate"].setText(f"{meta.bit_rate / 1_000_000:.2f} Mb/s" if meta.bit_rate else "-")
            d["Container"].setText(meta.format_name or "-")
            d["Audio"].setText(
                f"{meta.audio_codec}, {meta.sample_rate} Hz, {meta.channels} ch" if meta.has_audio else "No audio"
            )
            d["File size"].setText(format_size(meta.file_size))

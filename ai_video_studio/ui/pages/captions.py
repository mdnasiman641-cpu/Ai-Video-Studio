"""Captions page: generate, edit, style, preview, import/export and burn in captions."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QKeySequence, QPainter, QShortcut
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio.captions.correction import apply_suggestions
from ai_video_studio.captions.models import WHISPER_MODELS
from ai_video_studio.captions.model import (
    CaptionError,
    CaptionTrack,
    find_overlaps,
    format_clock,
    parse_clock,
)
from ai_video_studio.captions.srt import read_srt_file, write_srt_file
from ai_video_studio.captions.style import CaptionStyle
from ai_video_studio.core.errors import StudioError
from ai_video_studio.core.media import MediaItem, MediaStatus
from ai_video_studio.jobs.ai_jobs import AI_TITLES_JOB
from ai_video_studio.jobs.caption_jobs import BURN_IN_JOB, CORRECTION_JOB, FRAME_PREVIEW_JOB, TRANSCRIBE_JOB
from ai_video_studio.jobs.models import Job, JobState
from ai_video_studio.ui.caption_dialogs import (
    TitleSuggestionsDialog,
    ChooseVideosDialog,
    CorrectionReviewDialog,
    FramePreviewDialog,
    GenerateCaptionsDialog,
)
from ai_video_studio.ui.project_controller import JobBridge, ProjectController
from ai_video_studio.ui.widgets.caption_overlay import paint_captions
from ai_video_studio.ui.widgets.caption_style_editor import CaptionStyleEditor
from ai_video_studio.ui.widgets.common import muted, page_title
from ai_video_studio.ui.widgets.video_preview import VideoPreview

log = logging.getLogger(__name__)

COL_INDEX, COL_START, COL_END, COL_TEXT = range(4)
UNDO_LIMIT = 100
ACTIVE_ROW = QColor("#24324d")
OVERLAP_ROW = QColor("#4d3a1f")


class MultilineDelegate(QStyledItemDelegate):
    """Caption text editor: Enter adds a line break, Tab or clicking elsewhere commits."""

    def createEditor(self, parent, option, index):  # noqa: N802
        editor = QPlainTextEdit(parent)
        editor.setTabChangesFocus(True)
        return editor

    def setEditorData(self, editor, index):  # noqa: N802
        editor.setPlainText(index.data() or "")

    def setModelData(self, editor, model, index):  # noqa: N802
        model.setData(index, editor.toPlainText())

    def updateEditorGeometry(self, editor, option, index):  # noqa: N802
        rect = option.rect
        editor.setGeometry(rect.adjusted(0, 0, 0, max(0, 64 - rect.height())))


class CaptionsPage(QWidget):
    def __init__(self, controller: ProjectController, bridge: JobBridge):
        super().__init__()
        self.controller = controller
        self.services = controller.services
        self._media_id: str | None = None
        self._loaded_id: str | None = None
        self._refreshing = False
        self._undo: dict[str, list[dict | None]] = {}
        self._redo: dict[str, list[dict | None]] = {}
        self._preview_jobs: set[str] = set()
        self._active_rows: set[int] = set()

        # -- top bar --
        self.media_combo = QComboBox()
        self.media_combo.setMinimumWidth(260)
        self.generate_button = QPushButton("Generate Captions…")
        self.generate_button.setObjectName("Primary")
        self.import_button = QPushButton("Import SRT…")
        self.export_srt_button = QPushButton("Export SRT…")
        self.export_video_button = QPushButton("Export Video…")
        self.export_video_button.setToolTip("Export an MP4 with the captions burned in")
        self.exact_button = QPushButton("Exact Preview")
        self.exact_button.setToolTip("Render the current frame with FFmpeg exactly as it will be exported")
        self.ai_button = QPushButton("AI Check…")
        self.review_button = QPushButton("Review Suggestions")
        self.titles_button = QPushButton("Suggest Title…")
        self.titles_button.setToolTip("Ask an AI provider for a title, description and hashtags based on the "
                                      "caption text (shown for you to copy; nothing is changed)")
        top = QVBoxLayout()
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Video:"))
        row1.addWidget(self.media_combo, 1)
        row1.addWidget(self.generate_button)
        row1.addStretch(1)
        row2 = QHBoxLayout()
        for b in (self.import_button, self.export_srt_button, self.export_video_button,
                  self.exact_button, self.ai_button, self.review_button, self.titles_button):
            row2.addWidget(b)
        row2.addStretch(1)
        top.addLayout(row1)
        top.addLayout(row2)
        self.status = muted("")

        # -- preview --
        self.preview = VideoPreview()
        self.preview.set_volume(self.services.settings.get("preview_volume"))
        self.preview.set_overlay(self._paint_overlay)

        # -- caption table --
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "Start", "End", "Text"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setWordWrap(True)
        self.table.setItemDelegateForColumn(COL_TEXT, MultilineDelegate(self.table))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        header = self.table.horizontalHeader()
        for col in (COL_INDEX, COL_START, COL_END):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_TEXT, QHeaderView.ResizeMode.Stretch)

        self.add_button = QPushButton("Add at Playhead")
        self.split_button = QPushButton("Split at Playhead")
        self.merge_button = QPushButton("Merge")
        self.delete_button = QPushButton("Delete")
        self.undo_button = QPushButton("Undo")
        self.redo_button = QPushButton("Redo")
        edit_row = QHBoxLayout()
        for b in (self.add_button, self.split_button, self.merge_button, self.delete_button,
                  self.undo_button, self.redo_button):
            edit_row.addWidget(b)
        edit_row.addStretch(1)
        self.table_info = muted("")
        captions_tab = QWidget()
        cl = QVBoxLayout(captions_tab)
        cl.addLayout(edit_row)
        cl.addWidget(self.table, 1)
        cl.addWidget(self.table_info)
        cl.addWidget(muted("Double-click a cell to edit. Times: mm:ss.mmm. Select a row to jump there."))

        # -- style tab --
        self.preset_combo = QComboBox()
        self.apply_preset_button = QPushButton("Apply")
        self.save_preset_button = QPushButton("Save as Preset…")
        self.delete_preset_button = QPushButton("Delete Preset")
        self.preset_to_videos_button = QPushButton("Apply Preset to Videos…")
        self.style_to_videos_button = QPushButton("Copy This Style to Videos…")
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Preset:"))
        preset_row.addWidget(self.preset_combo, 1)
        preset_row.addWidget(self.apply_preset_button)
        preset_row2 = QHBoxLayout()
        for b in (self.save_preset_button, self.delete_preset_button, self.preset_to_videos_button,
                  self.style_to_videos_button):
            preset_row2.addWidget(b)
        preset_row2.addStretch(1)
        self.style_editor = CaptionStyleEditor()
        style_scroll = QScrollArea()
        style_scroll.setWidgetResizable(True)
        style_scroll.setWidget(self.style_editor)
        style_tab = QWidget()
        sl = QVBoxLayout(style_tab)
        sl.addLayout(preset_row)
        sl.addLayout(preset_row2)
        sl.addWidget(style_scroll, 1)

        self.tabs = QTabWidget()
        self.tabs.addTab(captions_tab, "Captions")
        self.tabs.addTab(style_tab, "Style")

        splitter = QSplitter()
        splitter.addWidget(self.preview)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        layout = QVBoxLayout(self)
        layout.addWidget(page_title("Captions"))
        layout.addLayout(top)
        layout.addWidget(self.status)
        layout.addWidget(splitter, 1)

        # -- connections --
        self.media_combo.currentIndexChanged.connect(lambda i: self._select_media(self.media_combo.itemData(i)))
        self.generate_button.clicked.connect(self.generate)
        self.import_button.clicked.connect(self.import_srt)
        self.export_srt_button.clicked.connect(self.export_srt)
        self.export_video_button.clicked.connect(self.export_video)
        self.exact_button.clicked.connect(self.exact_preview)
        self.ai_button.clicked.connect(self.ai_check)
        self.titles_button.clicked.connect(self.suggest_titles)
        self.review_button.clicked.connect(self.review_suggestions)
        self.add_button.clicked.connect(self.add_at_playhead)
        self.split_button.clicked.connect(self.split_at_playhead)
        self.merge_button.clicked.connect(self.merge_selected)
        self.delete_button.clicked.connect(self.delete_selected)
        self.undo_button.clicked.connect(self.undo)
        self.redo_button.clicked.connect(self.redo)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        self.preview.position_changed.connect(self._on_position)
        self.style_editor.style_changed.connect(self._on_style_edited)
        self.apply_preset_button.clicked.connect(self.apply_preset)
        self.save_preset_button.clicked.connect(self.save_preset)
        self.delete_preset_button.clicked.connect(self.delete_preset)
        self.preset_to_videos_button.clicked.connect(self.apply_preset_to_videos)
        self.style_to_videos_button.clicked.connect(self.copy_style_to_videos)
        self.preset_combo.currentIndexChanged.connect(lambda _i: self._update_buttons())
        for key, slot in (("Ctrl+Z", self.undo), ("Ctrl+Y", self.redo), ("Ctrl+Shift+Z", self.redo)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)
        delete_shortcut = QShortcut(QKeySequence.StandardKey.Delete, self.table)
        delete_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_shortcut.activated.connect(self.delete_selected)

        controller.media_list_changed.connect(self.refresh_media)
        controller.project_changed.connect(self._on_project_changed)
        controller.media_updated.connect(self._on_media_updated)
        controller.captions_changed.connect(self._on_captions_changed)
        controller.transcription_finished.connect(self._on_transcription_finished)
        controller.corrections_ready.connect(self._on_corrections_ready)
        bridge.job_changed.connect(self._on_job_changed)
        self.services.settings.subscribe(self._on_setting)
        self.reload_presets()
        self.refresh_media()

    # ------------------------------------------------------------------ state
    def current(self) -> MediaItem | None:
        project = self.controller.project
        return project.find_media(self._media_id) if project and self._media_id else None

    def track(self) -> CaptionTrack | None:
        item = self.current()
        return item.captions if item else None

    def refresh_media(self) -> None:
        project = self.controller.project
        self.media_combo.blockSignals(True)
        self.media_combo.clear()
        for item in project.media if project else []:
            self.media_combo.addItem(item.name, item.id)
        self.media_combo.blockSignals(False)
        index = self.media_combo.findData(self._media_id) if self._media_id else -1
        if index < 0 and self.media_combo.count():
            index = 0
        self.media_combo.setCurrentIndex(index)
        self._select_media(self.media_combo.itemData(index) if index >= 0 else None, force=True)

    def show_media(self, media_id: str) -> None:
        index = self.media_combo.findData(media_id)
        if index >= 0:
            self.media_combo.setCurrentIndex(index)

    def _on_project_changed(self) -> None:
        self._undo.clear()
        self._redo.clear()

    def _select_media(self, media_id: str | None, force: bool = False) -> None:
        if media_id == self._media_id and not force:
            return
        self._media_id = media_id
        self._sync_preview()
        self._load_style()
        self.refresh_table()
        self._update_status()
        self._update_buttons()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._sync_preview()
        self._update_buttons()

    def _sync_preview(self) -> None:
        """Load the clip into the player only while visible; unload immediately when it goes away."""
        if self._loaded_id == self._media_id:
            return
        if not self.isVisible() and self._media_id is not None:
            return
        self._loaded_id = self._media_id
        item = self.current()
        if item is None or item.status is MediaStatus.MISSING:
            self.preview.load(None)
        else:
            self.preview.load(item.path, item.metadata.fps if item.metadata else None)

    def stop_playback(self) -> None:
        self.preview.player.pause()

    def _on_media_updated(self, media_id: str) -> None:
        if media_id == self._media_id:
            item = self.current()
            if item and item.metadata:
                self.preview.set_frame_rate(item.metadata.fps)
            self._update_buttons()

    def _on_captions_changed(self, media_id: str) -> None:
        if media_id == self._media_id:
            self.refresh_table()
            self._load_style()
            self.preview.refresh_overlay()
            self._update_buttons()

    def _on_setting(self, key: str, value) -> None:
        if key == "preview_volume":
            self.preview.set_volume(value)

    # ------------------------------------------------------------------ table
    def refresh_table(self) -> None:
        track = self.track()
        segments = track.segments if track else []
        selected = self.selected_ids()
        overlapping = {b for _a, b in find_overlaps(segments)}
        self._refreshing = True
        try:
            self.table.setRowCount(len(segments))
            for row, seg in enumerate(segments):
                values = (str(row + 1), format_clock(seg.start), format_clock(seg.end), seg.text)
                for col, value in enumerate(values):
                    cell = QTableWidgetItem(value)
                    if col == COL_INDEX:
                        cell.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                        cell.setData(Qt.ItemDataRole.UserRole, seg.id)
                    if seg.id in overlapping:
                        cell.setBackground(QBrush(OVERLAP_ROW))
                        cell.setToolTip("Overlaps the previous caption")
                    self.table.setItem(row, col, cell)
            self.table.resizeRowsToContents()
            self.table.clearSelection()
            for row, seg in enumerate(segments):
                if seg.id in selected:
                    self.table.selectRow(row)
        finally:
            self._refreshing = False
        self._active_rows = set()
        if track is None:
            self.table_info.setText("No captions yet: generate them, import an SRT file, or add captions manually.")
        else:
            note = f" · {len(overlapping)} overlapping" if overlapping else ""
            language = f" · language: {track.language}" if track.language else ""
            self.table_info.setText(f"{len(segments)} caption(s){language} · source: {track.source}{note}")

    def selected_ids(self) -> list[str]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        ids = []
        for r in rows:
            item = self.table.item(r, COL_INDEX)
            if item is not None:
                ids.append(item.data(Qt.ItemDataRole.UserRole))
        return ids

    def _on_item_changed(self, cell: QTableWidgetItem) -> None:
        if self._refreshing or cell.column() == COL_INDEX:
            return
        seg_id = self.table.item(cell.row(), COL_INDEX).data(Qt.ItemDataRole.UserRole)
        track = self.track()
        seg = track.find(seg_id)
        duration = self.current().metadata.duration if self.current().metadata else None

        def edit(t: CaptionTrack) -> None:
            if cell.column() == COL_TEXT:
                t.set_text(seg_id, cell.text())
            else:
                start = parse_clock(cell.text()) if cell.column() == COL_START else seg.start
                end = parse_clock(cell.text()) if cell.column() == COL_END else seg.end
                t.set_times(seg_id, start, end, duration)

        if not self._edit(edit):
            self.refresh_table()  # revert the cell to the stored value

    def _on_row_selected(self) -> None:
        self._update_buttons()
        ids = self.selected_ids()
        track = self.track()
        if self._refreshing or len(ids) != 1 or track is None:
            return
        if self.preview.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            seg = track.find(ids[0])
            self.preview.seek(int(seg.start * 1000) + 1)

    def _on_position(self, ms: int) -> None:
        self.preview.refresh_overlay()
        track = self.track()
        if track is None:
            return
        active_ids = {s.id for s in track.active_at(ms / 1000)}
        rows = {r for r in range(self.table.rowCount())
                if self.table.item(r, COL_INDEX).data(Qt.ItemDataRole.UserRole) in active_ids}
        if rows == self._active_rows:
            return
        self._refreshing = True
        try:
            for r in self._active_rows - rows:
                if r < self.table.rowCount():
                    self.table.item(r, COL_INDEX).setBackground(QBrush())
            for r in rows:
                self.table.item(r, COL_INDEX).setBackground(QBrush(ACTIVE_ROW))
        finally:
            self._refreshing = False
        self._active_rows = rows

    # ------------------------------------------------------------------ editing
    def _edit(self, operation, create: bool = False) -> bool:
        """Run ``operation(track)`` on a copy, keeping an undo snapshot. Returns success."""
        item = self.current()
        if item is None:
            return False
        if item.captions is None and not create:
            return False
        before = item.captions.to_dict() if item.captions else None
        working = item.captions.copy() if item.captions else CaptionTrack(
            language=None if self.services.settings.get("transcription_language") == "auto"
            else self.services.settings.get("transcription_language"))
        try:
            operation(working)
        except (CaptionError, StudioError) as exc:
            self.status.setText(f"⚠ {exc}")
            return False
        stack = self._undo.setdefault(item.id, [])
        stack.append(before)
        del stack[:-UNDO_LIMIT]
        self._redo[item.id] = []
        self.controller.set_captions(item.id, working)
        self.status.setText("")
        return True

    def add_at_playhead(self) -> None:
        item = self.current()
        if item is None:
            return
        duration = item.metadata.duration if item.metadata else None
        at = self.preview.position_seconds()
        if self._edit(lambda t: t.add(at, text="New caption", video_duration=duration), create=True):
            self._select_ids([s.id for s in self.track().active_at(at)][-1:])

    def split_at_playhead(self) -> None:
        track = self.track()
        if track is None:
            return
        at = self.preview.position_seconds()
        ids = self.selected_ids()
        targets = [track.find(i) for i in ids] if len(ids) == 1 else track.active_at(at)
        targets = [s for s in targets if s.start < at < s.end]
        if not targets:
            self.status.setText("⚠ Move the playhead inside a caption to split it.")
            return
        self._edit(lambda t: t.split(targets[0].id, at_time=at))

    def merge_selected(self) -> None:
        ids = self.selected_ids()
        self._edit(lambda t: t.merge(ids))

    def delete_selected(self) -> None:
        ids = set(self.selected_ids())
        if ids:
            self._edit(lambda t: t.delete(ids))

    def undo(self) -> None:
        self._step(self._undo, self._redo, "Nothing to undo")

    def redo(self) -> None:
        self._step(self._redo, self._undo, "Nothing to redo")

    def _step(self, source: dict, target: dict, empty_message: str) -> None:
        item = self.current()
        if item is None or not source.get(item.id):
            self.status.setText(empty_message)
            return
        target.setdefault(item.id, []).append(item.captions.to_dict() if item.captions else None)
        snapshot = source[item.id].pop()
        self.controller.set_captions(item.id, CaptionTrack.from_dict(snapshot) if snapshot is not None else None)
        self.status.setText("")

    def _select_ids(self, ids: list[str]) -> None:
        self.table.clearSelection()
        for r in range(self.table.rowCount()):
            if self.table.item(r, COL_INDEX).data(Qt.ItemDataRole.UserRole) in ids:
                self.table.selectRow(r)

    # ------------------------------------------------------------------ overlay
    def _paint_overlay(self, painter: QPainter, rect: QRectF, size: QSize) -> None:
        item = self.current()
        if item is None or item.captions is None:
            return
        texts = [s.text for s in item.captions.active_at(self.preview.position_seconds())]
        paint_captions(painter, rect, texts, item.effective_caption_style)

    # ------------------------------------------------------------------ style & presets
    def _load_style(self) -> None:
        item = self.current()
        self.style_editor.set_style(item.effective_caption_style if item else CaptionStyle())

    def _on_style_edited(self, style: CaptionStyle) -> None:
        item = self.current()
        if item is None:
            return
        try:
            self.controller.set_caption_style([item.id], style)
        except StudioError as exc:
            self.status.setText(f"⚠ {exc}")

    def reload_presets(self, select: str | None = None) -> None:
        current = select or self.preset_combo.currentData()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for name, builtin in self.services.presets.names():
            self.preset_combo.addItem(f"{name} (built-in)" if builtin else name, name)
        self.preset_combo.blockSignals(False)
        index = self.preset_combo.findData(current) if current else -1
        self.preset_combo.setCurrentIndex(max(0, index))
        self._update_buttons()

    def _preset_style(self) -> CaptionStyle | None:
        name = self.preset_combo.currentData()
        if not name:
            return None
        try:
            return self.services.presets.get(name)
        except StudioError as exc:
            QMessageBox.warning(self, "Preset", str(exc))
            return None

    def apply_preset(self) -> None:
        style = self._preset_style()
        item = self.current()
        if style and item:
            self.controller.set_caption_style([item.id], style)
            self.status.setText(f"Applied preset “{self.preset_combo.currentData()}”.")

    def save_preset(self) -> None:
        name, ok = QInputDialog.getText(self, "Save Caption Preset", "Preset name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        if self.services.presets.exists(name):
            if QMessageBox.question(self, "Save Caption Preset", f"Replace the preset “{name}”?") \
                    != QMessageBox.StandardButton.Yes:
                return
        try:
            self.services.presets.save(name, self.style_editor.style())
        except StudioError as exc:
            QMessageBox.warning(self, "Save Caption Preset", str(exc))
            return
        self.reload_presets(select=name)
        self.status.setText(f"Saved preset “{name}”.")

    def delete_preset(self) -> None:
        name = self.preset_combo.currentData()
        if not name:
            return
        if QMessageBox.question(self, "Delete Preset", f"Delete the preset “{name}”?") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            self.services.presets.delete(name)
        except StudioError as exc:
            QMessageBox.warning(self, "Delete Preset", str(exc))
            return
        self.reload_presets()

    def apply_preset_to_videos(self) -> None:
        style = self._preset_style()
        if style is not None:
            self._style_to_videos(style, f"Apply preset “{self.preset_combo.currentData()}” to:")

    def copy_style_to_videos(self) -> None:
        self._style_to_videos(self.style_editor.style(), "Copy the current caption style to:")

    def _style_to_videos(self, style: CaptionStyle, prompt: str) -> None:
        project = self.controller.project
        if project is None:
            return
        dialog = ChooseVideosDialog("Caption Style", prompt, project.media, {self._media_id} if self._media_id else set(),
                                    self)
        if dialog.exec() != ChooseVideosDialog.DialogCode.Accepted:
            return
        ids = dialog.videos.checked_ids()
        self.apply_style_to(ids, style)

    def apply_style_to(self, media_ids: list[str], style: CaptionStyle) -> None:
        if not media_ids:
            return
        self.controller.set_caption_style(media_ids, style)
        self.status.setText(f"Caption style applied to {len(media_ids)} video(s).")

    # ------------------------------------------------------------------ actions
    def generate(self) -> None:
        project = self.controller.project
        if project is None or not project.media:
            return
        dialog = GenerateCaptionsDialog(project.media, self._media_id, self.services.settings, self)
        if dialog.exec() != GenerateCaptionsDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        missing = self.controller.model_to_download(values["model"])
        if missing is not None:
            spec = WHISPER_MODELS[missing]
            answer = QMessageBox.question(
                self, "Download Speech Model",
                f"The Whisper model '{missing}' is not on this computer yet.\n\n"
                f"Download it now from Hugging Face (about {spec.approx_mb} MB)? It is stored in your user data "
                f"folder, checked, and reused for all later transcriptions, also offline.\n\n"
                f"Progress is shown in Export Queue.")
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            jobs = self.controller.submit_transcription(values["media_ids"], values["language"], values["model"],
                                                        values["device"], allow_download=missing is not None)
        except StudioError as exc:
            QMessageBox.warning(self, "Generate Captions", str(exc))
            return
        self.status.setText(f"Queued caption generation for {len(jobs)} video(s). Progress is shown here and in "
                            "Export Queue.")
        self._update_buttons()

    def import_srt(self) -> None:
        item = self.current()
        if item is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import SRT", str(Path(item.path).parent),
                                              "SubRip subtitles (*.srt);;All files (*)")
        if path:
            self.import_srt_file(path)

    def import_srt_file(self, path: str) -> bool:
        item = self.current()
        try:
            result = read_srt_file(path)
        except (OSError, StudioError) as exc:
            QMessageBox.warning(self, "Import SRT", f"Could not import the file:\n{exc}")
            return False
        if item.captions and item.captions.segments:
            answer = QMessageBox.question(self, "Import SRT",
                                          f"Replace the {len(item.captions.segments)} existing caption(s)?")
            if answer != QMessageBox.StandardButton.Yes:
                return False

        def replace(t: CaptionTrack) -> None:
            t.segments = result.segments
            t.source = "srt"
            t.pending_corrections = []

        self._edit(replace, create=True)
        message = f"Imported {len(result.segments)} caption(s) from {Path(path).name}."
        duration = item.metadata.duration if item.metadata else None
        late = sum(1 for s in result.segments if duration and s.start >= duration)
        if late:
            message += f" {late} start after the end of the video."
        if result.warnings:
            message += f" {len(result.warnings)} block(s) skipped."
            QMessageBox.information(self, "Import SRT", message + "\n\n" + "\n".join(result.warnings[:15]))
        self.status.setText(message)
        return True

    def export_srt(self) -> None:
        item = self.current()
        if item is None or not item.captions or not item.captions.segments:
            return
        suggested = str(Path(item.path).with_suffix(".srt"))
        path, _ = QFileDialog.getSaveFileName(self, "Export SRT", suggested, "SubRip subtitles (*.srt)")
        if not path:
            return
        try:
            saved = write_srt_file(path, item.captions)
        except OSError as exc:
            QMessageBox.warning(self, "Export SRT", f"Could not write the file:\n{exc}")
            return
        self.status.setText(f"Saved {saved}")

    def export_video(self) -> None:
        item = self.current()
        if item is None:
            return
        source = Path(item.path)
        suggested = str(source.with_name(f"{source.stem}_captioned.mp4"))
        path, _ = QFileDialog.getSaveFileName(self, "Export Video with Captions", suggested, "MP4 video (*.mp4)")
        if not path:
            return
        if Path(path).suffix.lower() != ".mp4":
            path += ".mp4"
        self.start_export(path)

    def start_export(self, output: str) -> Job | None:
        try:
            job = self.controller.submit_burn_in(self._media_id, output)
        except StudioError as exc:
            QMessageBox.warning(self, "Export Video", str(exc))
            return None
        self.status.setText(f"Export queued: {Path(output).name}")
        return job

    def exact_preview(self) -> Job | None:
        try:
            job = self.controller.submit_frame_preview(self._media_id, self.preview.position_seconds())
        except StudioError as exc:
            QMessageBox.warning(self, "Exact Frame Preview", str(exc))
            return None
        self._preview_jobs.add(job.id)
        self.status.setText("Rendering exact preview with FFmpeg…")
        return job

    def _ai_disclosure(self, capability: str, what: str) -> bool:
        profiles, skipped = self.services.ai_plan(capability)
        if not profiles:
            QMessageBox.information(self, "AI", "No AI provider is ready for this. Set one up on the AI Models "
                                    "page (provider, model and API key).\n\n" + "\n".join(skipped))
            return False
        primary = f"{profiles[0].display_name} ({profiles[0].model})"
        fallbacks = ", ".join(f"{p.display_name} ({p.model})" for p in profiles[1:])
        text = (f"Send {what} to {primary}?" + (f"\n\nIf it fails, the same text may be sent to: {fallbacks}."
                                                 if fallbacks else "")
                + "\n\nOnly caption text is sent (no audio or video). This may cost API credits. "
                  "Nothing changes until you review the result.")
        return QMessageBox.question(self, "AI", text) == QMessageBox.StandardButton.Yes

    def ai_check(self) -> None:
        count = len(self.track().segments) if self.track() else 0
        if not self._ai_disclosure("caption_correction", f"the text of {count} caption(s) for spelling and "
                                                        "recognition fixes"):
            return
        try:
            self.controller.submit_correction(self._media_id)
        except StudioError as exc:
            QMessageBox.warning(self, "AI Check", str(exc))
            return
        self.status.setText("AI check queued…")
        self._update_buttons()

    def review_suggestions(self) -> None:
        track = self.track()
        if track is None or not track.pending_corrections:
            return
        dialog = CorrectionReviewDialog(track.pending_corrections, self)
        dialog.exec()
        self.finish_review(dialog.result_action, dialog.accepted_ids())

    def finish_review(self, action: int, accepted: set[str]) -> None:
        track = self.track()
        if action == CorrectionReviewDialog.APPLY:
            result = {}

            def apply(t: CaptionTrack) -> None:
                result["counts"] = apply_suggestions(t.segments, t.pending_corrections, accepted)
                t.pending_corrections = []

            if self._edit(apply):
                applied, stale = result["counts"]
                extra = f" ({stale} skipped because the caption was edited since the check)" if stale else ""
                self.status.setText(f"Applied {applied} AI suggestion(s){extra}.")
        elif action == CorrectionReviewDialog.DISCARD and track is not None:
            self._edit(lambda t: setattr(t, "pending_corrections", []))
            self.status.setText("AI suggestions discarded.")

    # ------------------------------------------------------------------ jobs
    def _on_transcription_finished(self, media_id: str, job: Job) -> None:
        if media_id != self._media_id:
            return
        self._undo.pop(media_id, None)
        self._redo.pop(media_id, None)
        if job.state is JobState.COMPLETED and job.result:
            warnings = job.result.get("warnings") or []
            text = (f"Captions generated with Whisper '{job.result.get('model')}' on "
                    f"{job.result.get('device', '?').upper()} · language {job.result.get('language')}.")
            if warnings:
                text += " ⚠ " + " ".join(warnings)
            self.status.setText(text)
        self._update_buttons()

    def _on_corrections_ready(self, media_id: str) -> None:
        if media_id == self._media_id:
            count = len(self.track().pending_corrections) if self.track() else 0
            self.status.setText(f"AI check finished: {count} suggestion(s) to review." if count
                                else "AI check finished: no changes suggested.")
            self._update_buttons()

    def _on_job_changed(self, job: Job) -> None:
        if job.id in self._preview_jobs and job.state.is_finished:
            self._preview_jobs.discard(job.id)
            if job.state is JobState.COMPLETED:
                self.status.setText("")
                FramePreviewDialog(job.result["image"], job.result["at"], self).exec()
                try:
                    Path(job.result["image"]).unlink()
                except OSError:
                    pass
            elif job.state is JobState.FAILED:
                self.status.setText(f"⚠ Preview failed: {job.error}")
            return
        if job.payload.get("media_id") != self._media_id or job.kind == FRAME_PREVIEW_JOB:
            return
        if job.kind in (TRANSCRIBE_JOB, BURN_IN_JOB, CORRECTION_JOB):
            self._update_status(job)
            self._update_buttons()
        elif job.kind == AI_TITLES_JOB and job.state.is_finished:
            if job.state is JobState.COMPLETED and job.result:
                self.status.setText(f"Suggestions from {job.result['provider']} ({job.result['model']}).")
                TitleSuggestionsDialog(job.result, self).exec()
            elif job.state is JobState.FAILED:
                self.status.setText(f"⚠ Title suggestion failed: {job.error}")

    def suggest_titles(self) -> Job | None:
        if not self._ai_disclosure("titles", "the caption text of this video for a title and description"):
            return None
        try:
            job = self.controller.submit_titles(self._media_id)
        except StudioError as exc:
            QMessageBox.warning(self, "Suggest Title", str(exc))
            return None
        self.status.setText("Asking for a title and description…")
        return job

    def _update_status(self, job: Job | None = None) -> None:
        labels = {TRANSCRIBE_JOB: "Generating captions", BURN_IN_JOB: "Exporting video",
                  CORRECTION_JOB: "AI check"}
        if job is None:
            self.status.setText("")
            return
        label = labels.get(job.kind, job.kind)
        if job.state is JobState.RUNNING:
            self.status.setText(f"{label}… {int(job.progress * 100)}%")
        elif job.state is JobState.QUEUED:
            self.status.setText(f"{label}: queued")
        elif job.state is JobState.FAILED:
            self.status.setText(f"⚠ {label} failed: {job.error}")
        elif job.state is JobState.CANCELLED:
            self.status.setText(f"{label} cancelled.")
        elif job.state is JobState.COMPLETED and job.kind == BURN_IN_JOB:
            warnings = " ⚠ " + " ".join(job.result.get("warnings", [])) if job.result.get("warnings") else ""
            self.status.setText(f"Exported {job.result['output']} ({job.result['encoder']}).{warnings}")

    def _update_buttons(self) -> None:
        item = self.current()
        has_item = item is not None
        usable = has_item and item.status is not MediaStatus.MISSING
        track = item.captions if item else None
        has_caps = bool(track and track.segments)
        ready = usable and item.metadata is not None
        transcribing = has_item and bool(self.controller.active_jobs(item.id, TRANSCRIBE_JOB))
        checking = has_item and bool(self.controller.active_jobs(item.id, CORRECTION_JOB))
        ids = self.selected_ids()

        self.generate_button.setEnabled(bool(self.controller.project and self.controller.project.media))
        self.import_button.setEnabled(has_item and not transcribing)
        self.export_srt_button.setEnabled(has_caps)
        self.export_video_button.setEnabled(has_caps and ready)
        self.exact_button.setEnabled(has_caps and ready)
        ai_ready = bool(self.services.ai_plan("caption_correction")[0])
        self.ai_button.setEnabled(has_caps and ai_ready and not checking)
        self.ai_button.setToolTip("" if ai_ready else
                                  "Optional. Set up an AI provider with caption correction on the AI Models page.")
        titles_ready = bool(self.services.ai_plan("titles")[0])
        self.titles_button.setEnabled(has_caps and titles_ready)
        self.titles_button.setToolTip(self.titles_button.toolTip() if titles_ready else
                                      "Optional. Set up an AI provider with title generation on the AI Models page.")
        pending = len(track.pending_corrections) if track else 0
        self.review_button.setEnabled(pending > 0)
        self.review_button.setText(f"Review Suggestions ({pending})" if pending else "Review Suggestions")
        self.add_button.setEnabled(usable and not transcribing)
        self.split_button.setEnabled(has_caps and not transcribing)
        self.merge_button.setEnabled(len(ids) >= 2 and not transcribing)
        self.delete_button.setEnabled(bool(ids) and not transcribing)
        self.table.setEnabled(not transcribing)
        self.undo_button.setEnabled(has_item and bool(self._undo.get(item.id)))
        self.redo_button.setEnabled(has_item and bool(self._redo.get(item.id)))
        name = self.preset_combo.currentData()
        builtin = dict(self.services.presets.names()).get(name, True) if name else True
        self.delete_preset_button.setEnabled(not builtin)
        for w in (self.apply_preset_button, self.style_editor, self.save_preset_button, self.style_to_videos_button):
            w.setEnabled(has_item)
        self.preset_to_videos_button.setEnabled(bool(self.controller.project and self.controller.project.media))

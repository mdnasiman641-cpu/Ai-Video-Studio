"""Smart Cut page: detect silences, review them, apply cuts and export the edited video.

The audio is analysed once in a background job; the proposal is then rebuilt instantly
from the stored levels whenever a setting changes. Every change is undoable. Nothing is cut
until **Apply Cuts**; exports use the applied cuts, the source video is never modified, and
captions are remapped to the edited timeline at export time (the caption track itself keeps
its original timing, so manual caption edits are never lost).
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio.captions.model import format_clock, parse_clock
from ai_video_studio.captions.srt import to_srt
from ai_video_studio.core import paths
from ai_video_studio.core.errors import StudioError
from ai_video_studio.core.media import MediaItem, MediaStatus, format_duration
from ai_video_studio.jobs.models import Job, JobState
from ai_video_studio.jobs.render_jobs import RENDER_JOB, SMARTCUT_ANALYZE_JOB
from ai_video_studio.render.pipeline import kept_duration
from ai_video_studio.smartcut.levels import suggest_threshold
from ai_video_studio.smartcut.model import (
    CutSegment,
    EditHistory,
    SmartCutSettings,
    SmartCutState,
    detection_notes,
    set_bounds,
    set_remove,
)
from ai_video_studio.ui.project_controller import JobBridge, ProjectController
from ai_video_studio.ui.widgets.common import Card, muted, open_folder, page_title, section_title
from ai_video_studio.ui.widgets.cut_timeline import CutTimeline
from ai_video_studio.ui.widgets.output_settings import ExportDialog
from ai_video_studio.ui.widgets.video_preview import VideoPreview

log = logging.getLogger(__name__)

COLUMNS = ["#", "Type", "Start", "End", "Length", "Action", "Note"]
COL_START, COL_END, COL_ACTION = 2, 3, 5


def _spin(low: float, high: float, step: float, suffix: str, decimals: int = 2) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(low, high)
    box.setSingleStep(step)
    box.setDecimals(decimals)
    box.setSuffix(suffix)
    box.setKeyboardTracking(False)
    return box


class SmartCutPage(QWidget):
    def __init__(self, controller: ProjectController, bridge: JobBridge):
        super().__init__()
        self.controller = controller
        self.services = controller.services
        self._media_id: str | None = None
        self._loaded_id: str | None = None
        self._histories: dict[str, EditHistory] = {}
        self._filling = False
        self._segment_stop: float | None = None   # stop playback here (segment preview)
        self._export_jobs: dict[str, str] = {}     # media id -> last export job id
        self._preview_jobs: set[str] = set()

        self.media_combo = QComboBox()
        self.media_combo.setMinimumWidth(320)
        self.durations = muted("", wrap=False)
        self.preview = VideoPreview()
        self.preview.set_volume(self.services.settings.get("preview_volume"))
        self.edited_playback = QCheckBox("Play edited result (skip removed parts)")
        self.timeline = CutTimeline()

        # settings
        self.threshold = _spin(-90, -5, 1, " dB", 0)
        self.min_silence = _spin(0.1, 30, 0.1, " s")
        self.pad_before = _spin(0, 5, 0.05, " s")
        self.pad_after = _spin(0, 5, 0.05, " s")
        self.min_keep = _spin(0, 10, 0.05, " s")
        self.merge_gap = _spin(0, 30, 0.1, " s")
        self.merge_gap.setSpecialValueText("Off")
        self.threshold.setToolTip("Audio quieter than this (peak level) counts as silence.")
        self.min_silence.setToolTip("Quiet parts shorter than this are natural pauses and are kept.")
        self.pad_before.setToolTip("Sound kept before speech starts, so words are not clipped.")
        self.pad_after.setToolTip("Sound kept after speech ends.")
        self.min_keep.setToolTip("Sounds shorter than this between two silences (clicks, breaths) are removed "
                                 "together with the silences. 0 keeps every sound.")
        self.merge_gap.setToolTip("Silences shorter than this after padding are kept, joining nearby speech.")
        self.suggest_button = QPushButton("Suggest Threshold")
        self.analyze_button = QPushButton("Analyze Audio")
        self.analyze_button.setObjectName("Primary")
        self.cancel_button = QPushButton("Cancel Analysis")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        self.status = muted("")
        self.notes = muted("")

        settings_card = Card()
        settings_card.body.addWidget(section_title("Silence detection"))
        form = QFormLayout()
        form.addRow("Silence threshold:", self.threshold)
        form.addRow("Minimum silence:", self.min_silence)
        form.addRow("Padding before speech:", self.pad_before)
        form.addRow("Padding after speech:", self.pad_after)
        form.addRow("Minimum kept sound:", self.min_keep)
        form.addRow("Merge speech closer than:", self.merge_gap)
        settings_card.body.addLayout(form)
        settings_card.body.addWidget(self.suggest_button)
        row = QHBoxLayout()
        row.addWidget(self.analyze_button)
        row.addWidget(self.cancel_button)
        settings_card.body.addLayout(row)
        settings_card.body.addWidget(self.progress)
        settings_card.body.addWidget(self.status)
        settings_card.body.addWidget(self.notes)
        settings_card.body.addStretch(1)
        settings_card.setMinimumWidth(300)

        # segments
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        self.keep_button = QPushButton("Keep Selected")
        self.remove_button = QPushButton("Remove Selected")
        self.select_all_button = QPushButton("Select All")
        self.deselect_button = QPushButton("Deselect All")
        self.undo_button = QPushButton("Undo")
        self.redo_button = QPushButton("Redo")
        self.play_segment_button = QPushButton("Preview Segment")
        self.apply_button = QPushButton("Apply Cuts")
        self.apply_button.setObjectName("Primary")
        self.reset_button = QPushButton("Reset Analysis")
        self.export_preview_button = QPushButton("Export Preview")
        self.export_button = QPushButton("Export Video…")
        self.export_srt_button = QPushButton("Export SRT…")
        self.cancel_export_button = QPushButton("Cancel Export")
        self.retry_export_button = QPushButton("Retry Export")
        self.open_folder_button = QPushButton("Open Output Folder")
        self.export_status = muted("")
        self.pending = QLabel("")
        self.pending.setObjectName("Warning")
        self.pending.setWordWrap(True)

        edit_row = QHBoxLayout()
        for b in (self.keep_button, self.remove_button, self.select_all_button, self.deselect_button,
                  self.undo_button, self.redo_button, self.play_segment_button):
            edit_row.addWidget(b)
        edit_row.addStretch(1)
        apply_row = QHBoxLayout()
        for b in (self.apply_button, self.reset_button, self.export_preview_button, self.export_button,
                  self.export_srt_button):
            apply_row.addWidget(b)
        apply_row.addStretch(1)
        export_row = QHBoxLayout()
        export_row.addWidget(self.export_status, 1)
        for b in (self.cancel_export_button, self.retry_export_button, self.open_folder_button):
            export_row.addWidget(b)

        player = QWidget()
        pl = QVBoxLayout(player)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.addWidget(self.preview, 1)
        pl.addWidget(self.edited_playback)
        top = QSplitter()
        top.addWidget(player)
        top.addWidget(settings_card)
        top.setStretchFactor(0, 3)
        top.setStretchFactor(1, 1)
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(self.timeline)
        bl.addWidget(self.pending)
        bl.addLayout(edit_row)
        bl.addWidget(self.table, 1)
        bl.addLayout(apply_row)
        bl.addLayout(export_row)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        header_row = QHBoxLayout()
        header_row.addWidget(QLabel("Video:"))
        header_row.addWidget(self.media_combo)
        header_row.addSpacing(16)
        header_row.addWidget(self.durations, 1)
        layout = QVBoxLayout(self)
        layout.addWidget(page_title("Smart Cut"))
        layout.addLayout(header_row)
        layout.addWidget(splitter, 1)

        self.media_combo.currentIndexChanged.connect(
            lambda i: self._select_media(self.media_combo.itemData(i) if i >= 0 else None))
        for box in (self.threshold, self.min_silence, self.pad_before, self.pad_after, self.min_keep, self.merge_gap):
            box.valueChanged.connect(self._on_settings_changed)
        self.suggest_button.clicked.connect(self.suggest)
        self.analyze_button.clicked.connect(self.analyze)
        self.cancel_button.clicked.connect(self.cancel_analysis)
        self.keep_button.clicked.connect(lambda: self.mark_selected(False))
        self.remove_button.clicked.connect(lambda: self.mark_selected(True))
        self.select_all_button.clicked.connect(self.table.selectAll)
        self.deselect_button.clicked.connect(self.table.clearSelection)
        self.undo_button.clicked.connect(self.undo)
        self.redo_button.clicked.connect(self.redo)
        self.play_segment_button.clicked.connect(self.preview_segment)
        self.apply_button.clicked.connect(self.apply_cuts)
        self.reset_button.clicked.connect(self.reset_analysis)
        self.export_preview_button.clicked.connect(self.export_preview)
        self.export_button.clicked.connect(self.export_video)
        self.export_srt_button.clicked.connect(self.export_srt)
        self.cancel_export_button.clicked.connect(self.cancel_export)
        self.retry_export_button.clicked.connect(self.retry_export)
        self.open_folder_button.clicked.connect(self.open_output_folder)
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.cellDoubleClicked.connect(self._on_double_click)
        self.timeline.seek_requested.connect(lambda t: self.preview.seek(int(t * 1000)))
        self.timeline.segment_clicked.connect(self._select_segment)
        self.preview.position_changed.connect(self._on_position)
        controller.media_list_changed.connect(self.refresh_media)
        controller.media_updated.connect(self._on_media_updated)
        controller.smartcut_changed.connect(self._on_state_changed)
        controller.smartcut_analysis_finished.connect(self._on_analysis_finished)
        controller.captions_changed.connect(lambda mid: mid == self._media_id and self._update_view())
        bridge.job_changed.connect(self._on_job_changed)
        self.services.settings.subscribe(self._on_setting)
        self.refresh_media()

    # ------------------------------------------------------------------ state
    def current(self) -> MediaItem | None:
        project = self.controller.project
        return project.find_media(self._media_id) if project and self._media_id else None

    def state(self) -> SmartCutState | None:
        return self.controller.smartcut_state(self._media_id) if self.current() else None

    def history(self) -> EditHistory:
        return self._histories.setdefault(self._media_id or "", EditHistory())

    def _store(self, state: SmartCutState) -> None:
        self.controller.set_smartcut_state(self._media_id, state)

    # ------------------------------------------------------------------ media selection
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

    def _select_media(self, media_id: str | None, force: bool = False) -> None:
        if media_id == self._media_id and not force:
            return
        self._media_id = media_id
        self._segment_stop = None
        self._sync_preview()
        self._load()
        self._show_active_analysis()
        self._show_export_job()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._sync_preview()

    def _sync_preview(self) -> None:
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

    def shutdown(self) -> None:
        self.preview.player.pause()

    def _on_setting(self, key: str, value) -> None:
        if key == "preview_volume":
            self.preview.set_volume(value)

    def _on_media_updated(self, media_id: str) -> None:
        if media_id == self._media_id:
            self._load()

    def _on_state_changed(self, media_id: str) -> None:
        if media_id == self._media_id and not self._filling:
            self._load()

    # ------------------------------------------------------------------ display
    def _load(self) -> None:
        state = self.state()
        self._filling = True
        try:
            s = state.settings if state else SmartCutSettings()
            for box, value in ((self.threshold, s.threshold_db), (self.min_silence, s.min_silence),
                               (self.pad_before, s.pad_before), (self.pad_after, s.pad_after),
                               (self.min_keep, s.min_keep), (self.merge_gap, s.merge_gap)):
                box.setValue(value)
            self._fill_table(state)
        finally:
            self._filling = False
        self._update_view()

    def _fill_table(self, state: SmartCutState | None) -> None:
        selected = self.selected_ids()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for n, seg in enumerate(state.segments if state else []):
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [str(n + 1), "Speech / sound" if seg.kind == "speech" else "Silence", format_clock(seg.start),
                      format_clock(seg.end), f"{seg.duration:.2f} s", "Remove" if seg.remove else "Keep", seg.note]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if col == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, seg.id)
                if col not in (COL_START, COL_END):
                    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                else:
                    cell.setToolTip("Double-click to change (mm:ss.mmm). The neighbouring segment adjusts.")
                if col == COL_ACTION:
                    cell.setForeground(Qt.GlobalColor.red if seg.remove else Qt.GlobalColor.green)
                self.table.setItem(row, col, cell)
            if seg.id in selected:
                self.table.selectRow(row)
        self.table.blockSignals(False)

    def _update_view(self) -> None:
        item, state = self.current(), self.state()
        meta = item.metadata if item else None
        duration = meta.duration if meta else 0.0
        self.timeline.set_data(state.levels if state else None, state.segments if state else [],
                               state.duration if state and state.segments else duration,
                               self.threshold.value())
        self.timeline.set_selected(self.selected_ids())
        if item is None:
            self.durations.setText("")
        else:
            parts = [f"Original {format_duration(duration)}"]
            if state and state.segments:
                proposal = kept_duration(state.proposal_ranges())
                parts.append(f"proposal {format_duration(proposal)} ({self._percent(proposal, duration)})")
            applied = state.cut_ranges() if state else None
            parts.append(f"export {format_duration(kept_duration(applied))} with cuts applied" if applied
                         else "no cuts applied")
            self.durations.setText(" · ".join(parts))
        notes = []
        if state and state.levels and state.segments:
            notes = detection_notes(state.levels, state.segments, state.settings)
        if item is not None and self.controller.smartcut_is_stale(item):
            notes.insert(0, "The video file changed after its audio was analysed. Analyse it again.")
        if item is not None and meta is not None and not meta.has_audio:
            notes.insert(0, "This video has no audio track, so silence cannot be detected.")
        self.notes.setText("\n".join(f"⚠ {n}" for n in notes))
        pending = []
        if state and state.has_pending_changes:
            pending.append("The reviewed proposal is not applied yet: exports use the "
                           + ("previously applied cuts." if state.cut_ranges() else "whole video."))
        if item is not None and item.captions and item.captions.segments and state and state.segments:
            kept = state.proposal_ranges()
            dropped = sum(1 for c in item.captions.segments
                          if not any(min(c.end, b) - max(c.start, a) > 0.05 for a, b in kept))
            if dropped:
                pending.append(f"{dropped} caption(s) lie entirely in removed parts and will not appear in exports "
                               "(the captions themselves are kept).")
        self.pending.setText(" ".join(pending))
        self._update_buttons()

    @staticmethod
    def _percent(part: float, whole: float) -> str:
        return "-" if not whole else f"−{(1 - part / whole):.0%}"

    def _update_buttons(self) -> None:
        item, state = self.current(), self.state()
        ready = item is not None and item.metadata is not None and item.status is not MediaStatus.MISSING
        analysing = bool(self._media_id and self.controller.active_jobs(self._media_id, SMARTCUT_ANALYZE_JOB))
        has_audio = bool(ready and item.metadata.has_audio)
        segments = bool(state and state.segments)
        selected = bool(self.selected_ids())
        self.analyze_button.setEnabled(ready and has_audio and not analysing)
        self.cancel_button.setEnabled(analysing)
        self.suggest_button.setEnabled(bool(state and state.levels))
        for b in (self.keep_button, self.remove_button, self.play_segment_button):
            b.setEnabled(segments and selected)
        self.select_all_button.setEnabled(segments)
        self.deselect_button.setEnabled(segments and selected)
        self.undo_button.setEnabled(self.history().can_undo)
        self.redo_button.setEnabled(self.history().can_redo)
        self.apply_button.setEnabled(segments and state.has_pending_changes)
        self.reset_button.setEnabled(bool(state and (state.levels or state.applied)))
        cuts = bool(state and state.cut_ranges())
        self.export_preview_button.setEnabled(ready and cuts)
        self.export_button.setEnabled(ready)
        self.export_srt_button.setEnabled(ready and bool(item.captions and item.captions.segments))
        self.edited_playback.setEnabled(segments)
        self._update_export_buttons()

    # ------------------------------------------------------------------ settings and editing
    def _settings_from_controls(self) -> SmartCutSettings:
        return SmartCutSettings(self.threshold.value(), self.min_silence.value(), self.pad_before.value(),
                                self.pad_after.value(), self.min_keep.value(), self.merge_gap.value())

    def _on_settings_changed(self) -> None:
        if self._filling or self.current() is None:
            return
        state = self.state()
        settings = self._settings_from_controls()
        try:
            settings.validate()
        except StudioError as exc:
            self.status.setText(f"⚠ {exc}")
            return
        if state.levels is not None:
            self.history().record(state.segments)  # re-detecting is undoable
            state.settings = settings
            state.detect()
        else:
            state.settings = settings
        self._filling = True
        try:
            self._store(state)
        finally:
            self._filling = False
        self._fill_table(state)
        self._update_view()

    def suggest(self) -> None:
        state = self.state()
        if state is None or state.levels is None:
            return
        threshold, message = suggest_threshold(state.levels)
        self.status.setText(message + f" Suggested threshold: {threshold:g} dB.")
        self.threshold.setValue(threshold)

    def selected_ids(self) -> set[str]:
        rows = {i.row() for i in self.table.selectionModel().selectedRows()}
        return {self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in rows if self.table.item(r, 0)}

    def _on_selection(self) -> None:
        self.timeline.set_selected(self.selected_ids())
        self._update_buttons()

    def _select_segment(self, seg_id: str) -> None:
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == seg_id:
                self.table.selectRow(row)
                self.table.scrollToItem(self.table.item(row, 0))
                return

    def _edit(self, change) -> bool:
        """Run ``change(segments)`` as one undoable edit; a rejected edit changes nothing."""
        state = self.state()
        if state is None or not state.segments:
            return False
        snapshot = [s.to_dict() for s in state.segments]
        try:
            change(state.segments)
        except StudioError as exc:
            state.segments = [CutSegment.from_dict(d) for d in snapshot]
            QMessageBox.warning(self, "Smart Cut", str(exc))
            self._fill_table(state)
            return False
        self.history().push(snapshot)
        self._filling = True
        try:
            self._store(state)
        finally:
            self._filling = False
        self._fill_table(state)
        self._update_view()
        return True

    def mark_selected(self, remove: bool) -> None:
        ids = self.selected_ids()
        if ids:
            self._edit(lambda segments: set_remove(segments, ids, remove))

    def _on_double_click(self, row: int, col: int) -> None:
        if col == COL_ACTION:
            seg_id = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            state = self.state()
            seg = next((s for s in state.segments if s.id == seg_id), None)
            if seg is not None:
                self._edit(lambda segments: set_remove(segments, {seg_id}, not seg.remove))

    def _on_item_changed(self, cell: QTableWidgetItem) -> None:
        if cell.column() not in (COL_START, COL_END):
            return
        row = cell.row()
        seg_id = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        try:
            start = parse_clock(self.table.item(row, COL_START).text())
            end = parse_clock(self.table.item(row, COL_END).text())
        except StudioError as exc:
            QMessageBox.warning(self, "Smart Cut", str(exc))
            self._fill_table(self.state())
            return
        self._edit(lambda segments: set_bounds(segments, seg_id, start, end))

    def undo(self) -> None:
        state = self.state()
        if state is not None and self.history().can_undo:
            state.segments = self.history().undo(state.segments)
            self._store(state)

    def redo(self) -> None:
        state = self.state()
        if state is not None and self.history().can_redo:
            state.segments = self.history().redo(state.segments)
            self._store(state)

    def apply_cuts(self) -> None:
        state = self.state()
        if state is None:
            return
        try:
            ranges = state.apply()
        except StudioError as exc:
            QMessageBox.warning(self, "Apply Cuts", str(exc))
            return
        self._store(state)
        removed = state.duration - kept_duration(ranges)
        self.status.setText(f"Cuts applied: {removed:.1f} s removed. Exports of this video now use these cuts; "
                            "the source file is unchanged.")

    def reset_analysis(self) -> None:
        if self.current() is None:
            return
        answer = QMessageBox.question(self, "Reset Analysis", "Remove the audio analysis, the proposal and the applied "
                                      "cuts for this video? The video file is not affected.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.history().clear()
        self.controller.clear_smartcut(self._media_id)

    # ------------------------------------------------------------------ playback
    def preview_segment(self) -> None:
        state = self.state()
        ids = self.selected_ids()
        segs = [s for s in state.segments if s.id in ids] if state else []
        if not segs:
            return
        self._segment_stop = segs[-1].end
        self.preview.seek(int(segs[0].start * 1000))
        self.preview.player.play()

    def _on_position(self, ms: int) -> None:
        t = ms / 1000
        self.timeline.set_position(t)
        if self._segment_stop is not None and t >= self._segment_stop:
            self._segment_stop = None
            self.preview.player.pause()
            return
        if self.edited_playback.isChecked() and self._segment_stop is None:
            state = self.state()
            seg = next((s for s in state.segments if s.start <= t < s.end), None) if state else None
            if seg is not None and seg.remove:
                self.preview.seek(int(seg.end * 1000) + 1)

    # ------------------------------------------------------------------ analysis job
    def analyze(self) -> Job | None:
        if self.current() is None:
            return None
        state = self.state()
        state.settings = self._settings_from_controls()
        try:
            job = self.controller.submit_smartcut_analysis(self._media_id)
        except StudioError as exc:
            QMessageBox.warning(self, "Analyze Audio", str(exc))
            return None
        self.status.setText("Audio analysis queued.")
        self._update_buttons()
        return job

    def cancel_analysis(self) -> None:
        for job in self.controller.active_jobs(self._media_id, SMARTCUT_ANALYZE_JOB) if self._media_id else []:
            try:
                self.services.jobs.cancel(job.id)
            except StudioError as exc:
                self.status.setText(f"⚠ {exc}")

    def _show_active_analysis(self) -> None:
        jobs = self.controller.active_jobs(self._media_id, SMARTCUT_ANALYZE_JOB) if self._media_id else []
        if jobs:
            self._show_analysis(jobs[0])
        else:
            self.progress.setVisible(False)
            self.status.setText("")

    def _on_analysis_finished(self, media_id: str, job: Job) -> None:
        if media_id == self._media_id:
            self.history().clear()
            self._load()
            self._show_analysis(job)

    def _show_analysis(self, job: Job) -> None:
        running = job.state in (JobState.QUEUED, JobState.RUNNING)
        self.progress.setVisible(running)
        self.progress.setValue(int(job.progress * 100))
        if job.state is JobState.RUNNING:
            self.status.setText(f"Analysing audio… {int(job.progress * 100)}%")
        elif job.state is JobState.QUEUED:
            self.status.setText("Audio analysis queued.")
        elif job.state is JobState.FAILED:
            self.status.setText(f"⚠ Analysis failed: {job.error}")
        elif job.state is JobState.CANCELLED:
            self.status.setText("Analysis cancelled.")
        elif job.state is JobState.COMPLETED:
            state = self.state()
            silences = sum(1 for s in state.segments if s.kind == "silence") if state else 0
            self.status.setText(f"Analysis finished: {silences} silence(s) proposed for removal. Review them, "
                                "then click Apply Cuts.")
        self._update_buttons()

    # ------------------------------------------------------------------ export
    def export_srt(self) -> None:
        item = self.current()
        if item is None:
            return
        segments = self.controller.edited_captions(self._media_id, self.controller.applied_cuts(self._media_id))
        if not segments:
            QMessageBox.information(self, "Export SRT", "No captions remain on the edited timeline.")
            return
        suggested = str(Path(item.path).with_name(f"{Path(item.path).stem}_edited.srt"))
        path, _ = QFileDialog.getSaveFileName(self, "Export SRT (edited timeline)", suggested, "SubRip (*.srt)")
        if not path:
            return
        try:
            Path(path).write_text(to_srt(segments), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Export SRT", f"Could not write the file: {exc}")
            return
        self.export_status.setText(f"Saved {Path(path).name} ({len(segments)} captions, edited timeline).")

    def export_video(self) -> Job | None:
        item, state = self.current(), self.state()
        if item is None:
            return None
        if state is not None and state.has_pending_changes:
            answer = QMessageBox.question(
                self, "Export Video", "The reviewed proposal has not been applied. Apply it now and export with "
                "these cuts?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel)
            if answer == QMessageBox.StandardButton.Cancel:
                return None
            if answer == QMessageBox.StandardButton.Yes:
                self.apply_cuts()
        cuts = self.controller.applied_cuts(self._media_id)
        notes = [f"{len(cuts)} kept part(s), {format_duration(kept_duration(cuts))} in total." if cuts
                 else "No cuts are applied: the whole video is exported."]
        effects = self.controller.effects(self._media_id).summary()
        if effects:
            notes.append("Speed and audio (Edit page): " + ", ".join(effects) + ".")
        source = Path(item.path)
        dialog = ExportDialog(self, "Export Edited Video", item.path, str(source.with_name(f"{source.stem}_cut.mp4")),
                              self.services.output_defaults(),
                              captions_available=bool(item.captions and item.captions.segments), notes=notes)
        if dialog.exec() != ExportDialog.DialogCode.Accepted:
            return None
        settings = dialog.form.settings()
        return self.start_export(dialog.output_path(), settings, dialog.burn_captions.isChecked(), dialog.overwrite)

    def start_export(self, output: str, settings, burn_captions: bool = False, overwrite: bool = False) -> Job | None:
        try:
            job = self.controller.submit_render(self._media_id, output, settings, use_cuts=True,
                                                burn_captions=burn_captions, overwrite=overwrite,
                                                title=f"Export cut video: {Path(output).name}")
            self.services.remember_output_settings(settings)
        except StudioError as exc:
            QMessageBox.warning(self, "Export Video", str(exc))
            return None
        self._export_jobs[self._media_id] = job.id
        self._show_export_job()
        return job

    def export_preview(self) -> Job | None:
        item = self.current()
        if item is None:
            return None
        output = paths.temp_dir() / f"smartcut-preview-{self._media_id}.mp4"
        settings = self.services.output_defaults()
        settings.codec, settings.encoder, settings.quality, settings.rate_control = "h264", "auto", "small", "quality"
        settings.fps, settings.all_audio_tracks = "original", False
        try:
            job = self.controller.submit_render(self._media_id, str(output), settings, use_cuts=True, overwrite=True,
                                                preview=True, title=f"Cut preview (360p): {item.name}")
        except StudioError as exc:
            QMessageBox.warning(self, "Export Preview", str(exc))
            return None
        self._preview_jobs.add(job.id)
        self.export_status.setText("Rendering a 360p preview of the edited video…")
        return job

    def _export_job(self) -> Job | None:
        job_id = self._export_jobs.get(self._media_id or "")
        if not job_id:
            return None
        try:
            return self.services.jobs.store.get(job_id)
        except KeyError:
            return None

    def _show_export_job(self) -> None:
        job = self._export_job()
        if job is None:
            self.export_status.setText("")
        elif job.state is JobState.QUEUED:
            self.export_status.setText(f"Export queued: {Path(job.payload['output']).name}")
        elif job.state is JobState.RUNNING:
            self.export_status.setText(f"Exporting {Path(job.payload['output']).name}… {int(job.progress * 100)}%")
        elif job.state is JobState.FAILED:
            self.export_status.setText(f"⚠ Export failed: {job.error}")
        elif job.state is JobState.CANCELLED:
            self.export_status.setText("Export cancelled.")
        elif job.state is JobState.COMPLETED and job.result:
            warnings = job.result.get("warnings") or []
            extra = (" ⚠ " + " ".join(warnings)) if warnings else ""
            self.export_status.setText(f"Exported {job.result['output']} ({job.result['encoder']}).{extra}")
        self._update_export_buttons()

    def _update_export_buttons(self) -> None:
        job = self._export_job()
        self.cancel_export_button.setEnabled(bool(job and job.state in (JobState.QUEUED, JobState.RUNNING)))
        self.retry_export_button.setEnabled(bool(job and job.state in (JobState.FAILED, JobState.CANCELLED)))
        self.open_folder_button.setEnabled(bool(job and job.state is JobState.COMPLETED))

    def cancel_export(self) -> None:
        job = self._export_job()
        if job is not None:
            try:
                self.services.jobs.cancel(job.id)
            except StudioError as exc:
                self.export_status.setText(f"⚠ {exc}")

    def retry_export(self) -> None:
        job = self._export_job()
        if job is not None:
            try:
                self.services.jobs.retry(job.id)
            except StudioError as exc:
                self.export_status.setText(f"⚠ {exc}")

    def open_output_folder(self) -> None:
        job = self._export_job()
        if job is not None and job.result:
            open_folder(Path(job.result["output"]).parent)

    def _on_job_changed(self, job: Job) -> None:
        if job.id in self._preview_jobs and job.state.is_finished:
            self._preview_jobs.discard(job.id)
            if job.state is JobState.COMPLETED:
                self.export_status.setText("Preview rendered; opening it in your video player.")
                QDesktopServices.openUrl(QUrl.fromLocalFile(job.result["output"]))
            elif job.state is JobState.FAILED:
                self.export_status.setText(f"⚠ Preview failed: {job.error}")
            return
        if job.kind == SMARTCUT_ANALYZE_JOB and job.payload.get("media_id") == self._media_id:
            self._show_analysis(job)
        elif job.kind == RENDER_JOB and job.id == self._export_jobs.get(self._media_id or ""):
            self._show_export_job()

"""Bulk Processing page: many videos, shared or per-video settings, one export job per video.

Each video gets its own ``render_video`` job in the Export Queue, so progress, cancel, retry
and crash recovery work exactly as for single exports. At most "Max concurrent renders"
(Settings, default 1) render at once. **Pause Batch** holds the videos that have not started;
the one being rendered finishes (cancel it to stop it now). The batch list itself is saved in
the user database, so it survives a restart or crash.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio.captions.models import installed_path
from ai_video_studio.captions.srt import read_srt_file
from ai_video_studio.core import paths
from ai_video_studio.core.errors import StudioError
from ai_video_studio.core.ffmpeg_tools import VideoMetadata
from ai_video_studio.core.media import format_duration, validate_video_path
from ai_video_studio.jobs.handlers import PROBE_JOB
from ai_video_studio.jobs.models import Job, JobState
from ai_video_studio.jobs.render_jobs import RENDER_FRAME_JOB, RENDER_JOB
from ai_video_studio.reframe.model import ReframeSettings
from ai_video_studio.render.batch import (
    Batch,
    BatchItem,
    build_payload,
    frame_preview_payload,
    item_progress,
    output_name,
    output_size,
)
from ai_video_studio.render.naming import CONFLICT_POLICIES, path_key, plan_outputs
from ai_video_studio.storage.settings import REFRAME_DEFAULT_KEYS
from ai_video_studio.ui.caption_dialogs import FramePreviewDialog
from ai_video_studio.ui.project_controller import JobBridge, ProjectController, expand_paths
from ai_video_studio.ui.widgets.common import Card, muted, open_folder, page_title, section_title
from ai_video_studio.ui.widgets.preset_editor import PresetDialog, PresetEditor

log = logging.getLogger(__name__)

COLUMNS = ["Video", "Duration", "Size", "Settings", "Output", "Status", "Progress", "Note"]
STATUS_LABELS = {
    "waiting": "Waiting", "held": "Waiting (paused)", "analyzing": "Analyzing", "ready": "Ready",
    "rendering": "Rendering", "completed": "Completed", "failed": "Failed", "cancelled": "Cancelled",
    "skipped": "Skipped",
}
ACTIVE = {JobState.QUEUED, JobState.RUNNING, JobState.HELD}


class BulkPage(QWidget):
    def __init__(self, controller: ProjectController, bridge: JobBridge):
        super().__init__()
        self.controller = controller
        self.services = controller.services
        self._probes: dict[str, str] = {}   # probe job id -> item id
        self._filling = False
        self.batch = self.services.batches.latest() or Batch()
        self.setAcceptDrops(True)

        self.add_button = QPushButton("Add Videos…")
        self.add_folder_button = QPushButton("Add Folder…")
        self.remove_button = QPushButton("Remove Selected")
        self.clear_button = QPushButton("Clear List")
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        self.customize_button = QPushButton("Customize Selected…")
        self.shared_button = QPushButton("Use Shared Settings")
        self.all_shared_button = QPushButton("Apply Shared Settings to All")
        self.all_shared_button.setToolTip("Every video uses the shared settings (removes per-video settings).")
        self.preview_button = QPushButton("Preview Selected")
        self.preview_button.setToolTip("Render one frame of the selected video with its settings (framing and "
                                       "caption style), without exporting the video.")
        self._preview_job: str | None = None
        self._preview_notes: list[str] = []

        self.preset_combo = QComboBox()
        self.apply_preset_button = QPushButton("Apply")
        self.save_preset_button = QPushButton("Save as Preset…")
        self.update_preset_button = QPushButton("Update Preset")
        self.rename_preset_button = QPushButton("Rename…")
        self.delete_preset_button = QPushButton("Delete")
        self.editor = PresetEditor(self.batch.shared, caption_presets=self.services.presets)
        self.output_dir = QLineEdit(self.batch.output_dir)
        self.output_dir.setPlaceholderText("Folder for the exported videos")
        self.browse_button = QPushButton("Browse…")
        self.conflict = QComboBox()
        for key, label in CONFLICT_POLICIES.items():
            self.conflict.addItem(label, key)
        self.conflict.setCurrentIndex(max(0, self.conflict.findData(self.batch.conflict)))
        self.settings_error = muted("")

        self.start_button = QPushButton("Start Batch")
        self.start_button.setObjectName("Primary")
        self.pause_button = QPushButton("Pause Batch")
        self.resume_button = QPushButton("Resume Batch")
        self.cancel_button = QPushButton("Cancel Selected")
        self.retry_button = QPushButton("Retry Failed")
        self.open_button = QPushButton("Open Output Folder")
        self.summary = muted("")
        self.overall = QProgressBar()
        self.overall.setRange(0, 100)

        tools = QHBoxLayout()
        for b in (self.add_button, self.add_folder_button, self.remove_button, self.clear_button):
            tools.addWidget(b)
        tools.addStretch(1)
        tools.addWidget(self.preview_button)
        tools.addWidget(self.customize_button)
        tools.addWidget(self.shared_button)
        tools.addWidget(self.all_shared_button)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addLayout(tools)
        ll.addWidget(self.table, 1)
        ll.addWidget(self.summary)
        ll.addWidget(self.overall)
        run = QHBoxLayout()
        for b in (self.start_button, self.pause_button, self.resume_button, self.cancel_button, self.retry_button,
                  self.open_button):
            run.addWidget(b)
        run.addStretch(1)
        ll.addLayout(run)

        card = Card()
        card.body.addWidget(section_title("Shared settings"))
        prow = QHBoxLayout()
        prow.addWidget(self.preset_combo, 1)
        prow.addWidget(self.apply_preset_button)
        card.body.addLayout(prow)
        prow2 = QHBoxLayout()
        for b in (self.save_preset_button, self.update_preset_button, self.rename_preset_button,
                  self.delete_preset_button):
            prow2.addWidget(b)
        card.body.addLayout(prow2)
        card.body.addWidget(self.editor)
        out = QFormLayout()
        orow = QHBoxLayout()
        orow.addWidget(self.output_dir, 1)
        orow.addWidget(self.browse_button)
        out.addRow("Output folder:", orow)
        out.addRow("If the file exists:", self.conflict)
        card.body.addLayout(out)
        card.body.addWidget(self.settings_error)
        card.body.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(card)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(400)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout = QVBoxLayout(self)
        layout.addWidget(page_title("Bulk Processing"))
        layout.addWidget(muted("Add videos, choose shared settings (or customise single videos) and start: every "
                               "video becomes an export job in the Export Queue. Source videos are never changed "
                               "or overwritten. Drag and drop videos or folders here."))
        layout.addWidget(splitter, 1)

        self.add_button.clicked.connect(self.add_dialog)
        self.add_folder_button.clicked.connect(self.add_folder_dialog)
        self.remove_button.clicked.connect(self.remove_selected)
        self.clear_button.clicked.connect(self.clear_list)
        self.customize_button.clicked.connect(self.customize_selected)
        self.shared_button.clicked.connect(self.use_shared_for_selected)
        self.all_shared_button.clicked.connect(self.apply_shared_to_all)
        self.preview_button.clicked.connect(self.preview_selected)
        self.apply_preset_button.clicked.connect(self.apply_preset)
        self.save_preset_button.clicked.connect(self.save_preset)
        self.update_preset_button.clicked.connect(self.update_preset)
        self.rename_preset_button.clicked.connect(self.rename_preset)
        self.delete_preset_button.clicked.connect(self.delete_preset)
        self.preset_combo.currentIndexChanged.connect(self._update_buttons)
        self.editor.changed.connect(self._on_shared_changed)
        self.output_dir.editingFinished.connect(self._on_output_changed)
        self.browse_button.clicked.connect(self._browse_output)
        self.conflict.currentIndexChanged.connect(self._on_output_changed)
        self.start_button.clicked.connect(self.start)
        self.pause_button.clicked.connect(self.pause)
        self.resume_button.clicked.connect(self.resume)
        self.cancel_button.clicked.connect(self.cancel_selected)
        self.retry_button.clicked.connect(self.retry_failed)
        self.open_button.clicked.connect(lambda: self.batch.output_dir and open_folder(self.batch.output_dir))
        self.table.itemSelectionChanged.connect(self._update_buttons)
        bridge.job_changed.connect(self._on_job_changed)
        self._fill_presets()
        self._probe_missing()
        self.refresh()

    # ------------------------------------------------------------------ persistence
    def save(self) -> None:
        self.services.batches.save(self.batch)

    def job(self, item: BatchItem) -> Job | None:
        if not item.job_id:
            return None
        try:
            return self.services.jobs.store.get(item.job_id)
        except KeyError:
            return None

    # ------------------------------------------------------------------ adding and removing
    def add_dialog(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Add Videos", "", "Videos (*.mp4 *.mov *.mkv *.avi *.webm *.m4v "
                                                "*.wmv *.flv *.mts *.m2ts *.ts *.mpg *.mpeg)")
        if files:
            self.add_paths(files)

    def add_folder_dialog(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add Folder")
        if folder:
            self.add_paths([folder])

    def add_paths(self, raw: list[str]) -> tuple[int, int, list[str]]:
        """Returns ``(added, duplicates, rejected messages)``."""
        valid, rejected = [], []
        for path in expand_paths(raw):
            error = validate_video_path(path)
            if error:
                rejected.append(f"{Path(path).name}: {error}")
            else:
                valid.append(path)
        added, duplicates = self.batch.add(valid)
        self.save()
        for item in added:
            self._probe(item)
        self.refresh()
        message = f"{len(added)} video(s) added"
        if duplicates:
            message += f", {len(duplicates)} already in the list"
        if rejected:
            message += f", {len(rejected)} skipped"
        self.summary.setText(message + ".")
        return len(added), len(duplicates), rejected

    def _probe(self, item: BatchItem) -> None:
        job = self.services.jobs.submit(PROBE_JOB, f"Read metadata: {item.name}",
                                        {"path": item.path, "batch_id": self.batch.id, "batch_item": item.id})
        self._probes[job.id] = item.id

    def _probe_missing(self) -> None:
        for item in self.batch.items:
            if item.metadata is None and item.error is None and Path(item.path).is_file():
                self._probe(item)

    def selected_items(self) -> list[BatchItem]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        items = []
        for r in rows:
            cell = self.table.item(r, 0)
            item = self.batch.find(cell.data(Qt.ItemDataRole.UserRole)) if cell else None
            if item is not None:
                items.append(item)
        return items

    def _active(self, item: BatchItem) -> bool:
        job = self.job(item)
        return job is not None and job.state in ACTIVE

    def remove_selected(self) -> None:
        items = self.selected_items()
        busy = [i.name for i in items if self._active(i)]
        if busy:
            QMessageBox.warning(self, "Remove", "Cancel these first, they are queued or rendering:\n" + "\n".join(busy))
            return
        self.batch.remove({i.id for i in items})
        self.save()
        self.refresh()

    def clear_list(self) -> None:
        if any(self._active(i) for i in self.batch.items):
            QMessageBox.warning(self, "Clear List", "Some videos are still queued or rendering. Cancel them first.")
            return
        if self.batch.items and QMessageBox.question(self, "Clear List", "Remove all videos from the list? "
                                                     "Exported files are kept.") != QMessageBox.StandardButton.Yes:
            return
        old = self.batch
        self.services.batches.delete(old.id)
        self.batch = Batch(shared=old.shared, preset_name=old.preset_name, output_dir=old.output_dir,
                           conflict=old.conflict)
        self.save()
        self.refresh()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.add_paths(paths)
        event.acceptProposedAction()

    # ------------------------------------------------------------------ settings and presets
    def _fill_presets(self, select: str | None = None) -> None:
        current = select or self.preset_combo.currentData() or self.batch.preset_name
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for name, builtin in self.services.render_presets.names():
            self.preset_combo.addItem(f"{name} (built-in)" if builtin else name, name)
        self.preset_combo.blockSignals(False)
        index = self.preset_combo.findData(current)
        self.preset_combo.setCurrentIndex(max(0, index))
        self._update_buttons()

    def selected_preset(self) -> str | None:
        return self.preset_combo.currentData()

    def apply_preset(self) -> None:
        name = self.selected_preset()
        if not name:
            return
        self.editor.set_preset(self.services.render_presets.get(name))
        self.batch.preset_name = name
        self._on_shared_changed()

    def use_shared_preset(self, preset) -> None:
        """Use settings made elsewhere (the Edit page) as the batch's shared settings."""
        self.editor.set_preset(preset)
        self.batch.preset_name = ""
        self._on_shared_changed()
        self.summary.setText("Shared settings taken from the Edit page. Add videos and start the batch.")

    def save_preset(self) -> None:
        try:
            preset = self.editor.preset()
        except StudioError as exc:
            QMessageBox.warning(self, "Save Preset", str(exc))
            return
        name, ok = QInputDialog.getText(self, "Save as Preset", "Preset name:")
        if not ok or not name.strip():
            return
        store = self.services.render_presets
        if store.exists(name.strip()) and QMessageBox.question(
                self, "Save as Preset", f"Replace the preset '{name.strip()}'?") != QMessageBox.StandardButton.Yes:
            return
        try:
            saved = store.save(name, preset)
        except StudioError as exc:
            QMessageBox.warning(self, "Save Preset", str(exc))
            return
        self.batch.preset_name = saved
        self.save()
        self._fill_presets(saved)

    def update_preset(self) -> None:
        name = self.selected_preset()
        if not name:
            return
        try:
            self.services.render_presets.save(name, self.editor.preset())
        except StudioError as exc:
            QMessageBox.warning(self, "Update Preset", str(exc))
            return
        self.summary.setText(f"Preset '{name}' updated.")

    def rename_preset(self) -> None:
        name = self.selected_preset()
        if not name:
            return
        new, ok = QInputDialog.getText(self, "Rename Preset", "New name:", text=name)
        if not ok:
            return
        try:
            new = self.services.render_presets.rename(name, new)
        except StudioError as exc:
            QMessageBox.warning(self, "Rename Preset", str(exc))
            return
        if self.batch.preset_name == name:
            self.batch.preset_name = new
            self.save()
        self._fill_presets(new)

    def delete_preset(self) -> None:
        name = self.selected_preset()
        if not name or QMessageBox.question(self, "Delete Preset", f"Delete the preset '{name}'?") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            self.services.render_presets.delete(name)
        except StudioError as exc:
            QMessageBox.warning(self, "Delete Preset", str(exc))
            return
        self._fill_presets()

    def _on_shared_changed(self) -> None:
        try:
            self.batch.shared = self.editor.preset()
            self.settings_error.setText("")
        except StudioError as exc:
            self.settings_error.setText(f"⚠ {exc}")
            return
        self.save()
        self.refresh()

    def _browse_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Output Folder", self.output_dir.text())
        if folder:
            self.output_dir.setText(folder)
            self._on_output_changed()

    def _on_output_changed(self, *_args) -> None:
        self.batch.output_dir = self.output_dir.text().strip()
        self.batch.conflict = self.conflict.currentData()
        self.save()
        self.refresh()

    def customize_selected(self) -> None:
        items = self.selected_items()
        if not items:
            return
        dialog = PresetDialog(self, f"Settings for {items[0].name}" + (f" and {len(items) - 1} more" if len(items) > 1
                                                                        else ""), self.batch.effective(items[0]),
                              caption_presets=self.services.presets)
        if dialog.exec() != PresetDialog.DialogCode.Accepted:
            return
        for item in items:
            item.override = dialog.result_preset.copy()
        self.save()
        self.refresh()

    def use_shared_for_selected(self) -> None:
        for item in self.selected_items():
            item.override = None
        self.save()
        self.refresh()

    def apply_shared_to_all(self, confirm: bool = True) -> None:
        custom = [i for i in self.batch.items if i.override is not None]
        if custom and confirm and QMessageBox.question(
                self, "Apply Shared Settings to All", f"{len(custom)} video(s) have their own settings. Use the "
                "shared settings for every video?") != QMessageBox.StandardButton.Yes:
            return
        for item in custom:
            item.override = None
        self.save()
        self.refresh()
        self.summary.setText(f"All {len(self.batch.items)} video(s) use the shared settings.")

    def preview_selected(self) -> Job | None:
        items = self.selected_items()
        if not items:
            return None
        item = items[0]
        captions, style = self.captions_for(item)
        try:
            payload, notes = frame_preview_payload(item, self.batch.effective(item), captions, style)
        except StudioError as exc:
            QMessageBox.warning(self, "Preview", str(exc))
            return None
        job = self.services.jobs.submit(RENDER_FRAME_JOB, f"Batch preview frame: {item.name}", payload)
        self._preview_job, self._preview_notes = job.id, notes
        self.summary.setText(f"Rendering a preview frame of {item.name}…")
        return job

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.editor.fill_caption_styles()  # caption presets may have changed on the Captions page

    # ------------------------------------------------------------------ status
    def status(self, item: BatchItem) -> tuple[str, float]:
        job = self.job(item)
        if job is None:
            if item.skipped:
                return "skipped", 0.0
            if item.error:
                return "failed", 0.0
            if item.metadata is None:
                return "analyzing", 0.0  # reading the video information
            return "ready", 0.0
        if job.state is JobState.QUEUED:
            return "waiting", 0.0
        if job.state is JobState.HELD:
            return "held", 0.0
        if job.state is JobState.RUNNING:
            return ("analyzing" if job.stage == "analyzing" else "rendering"), job.progress
        return job.state.value, (1.0 if job.state is JobState.COMPLETED else job.progress)

    def refresh(self) -> None:
        selected = {i.id for i in self.selected_items()}
        self.table.setRowCount(0)
        for item in self.batch.items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._fill_row(row, item)
            if item.id in selected:
                self.table.selectRow(row)
        self._update_summary()
        self._update_buttons()

    def _fill_row(self, row: int, item: BatchItem) -> None:
        status, progress = self.status(item)
        job = self.job(item)
        meta = item.metadata
        preset = self.batch.effective(item)
        size = "-"
        if meta is not None:
            try:
                w, h = output_size(preset, meta)
                size = f"{meta.display_width}×{meta.display_height} → {w}×{h}"
            except StudioError:
                size = meta.resolution_text
        note = item.note or item.error or ""
        if job is not None and job.state is JobState.FAILED:
            note = job.error or note
        elif job is not None and job.state is JobState.COMPLETED and job.result and job.result.get("warnings"):
            note = " ".join(job.result["warnings"])
        values = [item.name, format_duration(meta.duration) if meta else "-", size,
                  "Custom" if item.override else "Shared", Path(item.output).name if item.output else "",
                  STATUS_LABELS[status], None, note]
        for col, value in enumerate(values):
            if value is None:
                continue
            cell = QTableWidgetItem(value)
            if col == 0:
                cell.setData(Qt.ItemDataRole.UserRole, item.id)
                cell.setToolTip(item.path)
            if col == 7 and value:
                cell.setToolTip(value)
            if col == 5 and status == "failed":
                cell.setForeground(Qt.GlobalColor.red)
            self.table.setItem(row, col, cell)
        bar = self.table.cellWidget(row, 6)
        if not isinstance(bar, QProgressBar):
            bar = QProgressBar()
            bar.setRange(0, 100)
            self.table.setCellWidget(row, 6, bar)
        bar.setValue(int(progress * 100))

    def _update_summary(self) -> None:
        statuses = [self.status(i) for i in self.batch.items]
        counts: dict[str, int] = {}
        for status, _p in statuses:
            counts[status] = counts.get(status, 0) + 1
        started = [s for i, s in zip(self.batch.items, statuses) if i.job_id or i.skipped]
        ready = counts.get("ready", 0)
        parts = [f"{len(self.batch.items)} video(s)"]
        parts += [f"{n} {STATUS_LABELS[k].lower()}" for k, n in counts.items()]
        if ready:
            parts.append(f"{ready} output file(s) will be created when you start")
        self.summary.setText(" · ".join(parts))
        self.overall.setValue(int(item_progress(started) * 100))

    def _update_buttons(self) -> None:
        items = self.selected_items()
        jobs = [self.job(i) for i in self.batch.items]
        states = {j.state for j in jobs if j is not None}
        selected_states = {j.state for j in (self.job(i) for i in items) if j is not None}
        builtin = bool(self.selected_preset()) and dict(self.services.render_presets.names()).get(
            self.selected_preset(), True)
        self.remove_button.setEnabled(bool(items))
        self.clear_button.setEnabled(bool(self.batch.items))
        self.customize_button.setEnabled(bool(items))
        self.shared_button.setEnabled(any(i.override for i in items))
        self.all_shared_button.setEnabled(any(i.override for i in self.batch.items))
        self.preview_button.setEnabled(len(items) == 1 and items[0].metadata is not None and self._preview_job is None)
        self.start_button.setEnabled(any(self.status(i)[0] == "ready" for i in self.batch.items))
        self.pause_button.setEnabled(JobState.QUEUED in states)
        self.resume_button.setEnabled(JobState.HELD in states)
        self.cancel_button.setEnabled(bool(selected_states & ACTIVE))
        self.retry_button.setEnabled(bool(states & {JobState.FAILED, JobState.CANCELLED}))
        self.open_button.setEnabled(bool(self.batch.output_dir) and Path(self.batch.output_dir).is_dir())
        self.update_preset_button.setEnabled(not builtin)
        self.rename_preset_button.setEnabled(not builtin)
        self.delete_preset_button.setEnabled(not builtin)

    # ------------------------------------------------------------------ running
    def captions_for(self, item: BatchItem):
        """Captions from the open project (same file), else from a .srt next to the video."""
        project = self.controller.project
        if project is not None:
            media = next((m for m in project.media if path_key(m.path) == path_key(item.path)), None)
            if media is not None and media.captions and media.captions.segments:
                return media.captions.segments, media.effective_caption_style
        srt = Path(item.path).with_suffix(".srt")
        if srt.is_file():
            try:
                return read_srt_file(srt).segments, None
            except (OSError, StudioError) as exc:
                log.warning("Could not read sidecar subtitles", extra={"path": str(srt), "error": str(exc)})
        return None, None

    def reviewed_cuts_for(self, item: BatchItem):
        """Smart Cut cuts applied to the same video in the open project, if any."""
        project = self.controller.project
        if project is None:
            return None
        media = next((m for m in project.media if path_key(m.path) == path_key(item.path)), None)
        if media is None or self.controller.smartcut_is_stale(media):
            return None
        return self.controller.applied_cuts(media.id)

    def transcription_settings(self) -> dict:
        """Whisper settings for automatic captions: the same as Generate Captions on the Captions page."""
        settings = self.services.settings
        return {"model": settings.get("whisper_model"), "language": settings.get("transcription_language"),
                "device": settings.get("transcription_device"),
                "model_path": settings.get("whisper_model_dir") or None,
                "models_dir": str(paths.data_dir() / "models"), "max_chars": settings.get("caption_max_chars"),
                "max_seconds": settings.get("caption_max_seconds")}

    def missing_whisper_model(self) -> str | None:
        """Why automatic captions cannot run now (no installed model), or None."""
        t = self.transcription_settings()
        if t["model_path"]:
            return None
        try:
            installed = installed_path(t["model"]) is not None
        except OSError:
            installed = False
        if installed:
            return None
        return (f"Automatic captions use the Whisper model '{t['model']}' (Settings), which is not on this "
                "computer. Batches never download models: download it in Settings → Speech recognition models, "
                "or choose an installed local model folder in Settings, then start the batch again.")

    def tracking_defaults(self) -> ReframeSettings:
        settings = ReframeSettings()
        for key, name in REFRAME_DEFAULT_KEYS.items():
            setattr(settings, name, self.services.settings.get(key))
        return settings

    def start(self) -> list[Job]:
        out_dir = self.batch.output_dir
        if not out_dir:
            QMessageBox.warning(self, "Start Batch", "Choose an output folder first.")
            return []
        try:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "Start Batch", f"The output folder cannot be created: {exc}")
            return []
        ready = [i for i in self.batch.items if self.status(i)[0] == "ready"]
        if not ready:
            return []
        problems, planned = [], []
        for n, item in enumerate(self.batch.items):
            if item not in ready:
                continue
            try:
                self.batch.effective(item).validate()
                planned.append((item.id, Path(out_dir) / output_name(self.batch.effective(item), item, n + 1,
                                                                     self.batch.preset_name)))
            except StudioError as exc:
                problems.append(f"{item.name}: {exc}")
        if problems:
            QMessageBox.warning(self, "Start Batch", "Fix these settings first:\n\n" + "\n".join(problems))
            return []
        if any(self.batch.effective(i).auto_captions and self.batch.effective(i).resize.burn_captions
               for i in ready):
            reason = self.missing_whisper_model()
            if reason:
                QMessageBox.warning(self, "Start Batch", reason)
                return []
        busy = set(self.controller.active_outputs())
        sources = {path_key(i.path) for i in self.batch.items}
        if self.controller.project is not None:
            sources |= {path_key(m.path) for m in self.controller.project.media}
        decisions = plan_outputs(planned, self.batch.conflict, sources, busy)
        jobs, failures = [], []
        for decision in decisions:
            item = self.batch.find(decision.item_id)
            if decision.path is None:
                item.skipped, item.note = True, decision.reason
                continue
            captions, style = self.captions_for(item)
            try:
                payload, notes = build_payload(
                    item, self.batch.effective(item), decision.path, overwrite=decision.action == "replace",
                    batch_id=self.batch.id, captions=captions, caption_style=style,
                    tracking_defaults=self.tracking_defaults(), models_dir=str(paths.data_dir() / "models"),
                    reviewed_cuts=self.reviewed_cuts_for(item), transcription=self.transcription_settings())
            except StudioError as exc:
                failures.append(f"{item.name}: {exc}")
                item.note = str(exc)
                continue
            item.output, item.note = str(decision.path), " ".join(([decision.reason] if decision.reason else []) + notes)
            job = self.services.jobs.submit(RENDER_JOB, f"Batch: {item.name} → {decision.path.name}", payload,
                                            project_id=None)
            item.job_id = job.id
            jobs.append(job)
        self.save()
        self.refresh()
        if failures:
            QMessageBox.warning(self, "Start Batch", "Not started:\n\n" + "\n".join(failures))
        return jobs

    def _batch_jobs(self, states: set[JobState]) -> list[Job]:
        return [j for j in (self.job(i) for i in self.batch.items) if j is not None and j.state in states]

    def pause(self) -> None:
        for job in self._batch_jobs({JobState.QUEUED}):
            try:
                self.services.jobs.hold(job.id)
            except StudioError:
                pass  # it started in the meantime
        self.refresh()

    def resume(self) -> None:
        for job in self._batch_jobs({JobState.HELD}):
            self.services.jobs.release(job.id)
        self.refresh()

    def cancel_selected(self) -> None:
        for item in self.selected_items():
            job = self.job(item)
            if job is not None and job.state in ACTIVE:
                try:
                    self.services.jobs.cancel(job.id)
                except StudioError as exc:
                    log.warning("Cancel failed", extra={"job_id": job.id, "error": str(exc)})
        self.refresh()

    def retry_failed(self) -> None:
        for job in self._batch_jobs({JobState.FAILED, JobState.CANCELLED}):
            output = job.payload.get("output")
            if output and path_key(output) in self.controller.active_outputs():
                continue  # another job writes this file now
            self.services.jobs.retry(job.id)
        self.refresh()

    def _on_job_changed(self, job: Job) -> None:
        if job.kind == RENDER_FRAME_JOB and job.id == self._preview_job and job.state.is_finished:
            self._preview_job = None
            if job.state is JobState.COMPLETED and job.result:
                self.summary.setText("")
                note = " ".join(["Framing and caption style as they will be exported."] + self._preview_notes)
                FramePreviewDialog(job.result["image"], job.result["at"], self, title="Batch Preview",
                                   note=note).exec()
            else:
                self.summary.setText(f"⚠ Preview failed: {job.error or 'cancelled'}")
            return
        if job.kind == PROBE_JOB and job.payload.get("batch_id") == self.batch.id and job.state.is_finished:
            item = self.batch.find(job.payload.get("batch_item", ""))
            self._probes.pop(job.id, None)
            if item is not None:
                if job.state is JobState.COMPLETED and job.result:
                    item.metadata, item.error = VideoMetadata.from_dict(job.result["metadata"]), None
                else:
                    item.error = job.error or "Could not read the video."
                self.save()
                self.refresh()
            return
        if job.kind == RENDER_JOB and job.payload.get("batch_id") == self.batch.id:
            item = self.batch.find(job.payload.get("batch_item", ""))
            if item is None:
                return
            for row in range(self.table.rowCount()):
                if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == item.id:
                    self._fill_row(row, item)
            self._update_summary()
            self._update_buttons()

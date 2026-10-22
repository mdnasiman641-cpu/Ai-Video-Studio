"""Bulk Processing page: add many videos, set the editing operations once, export them all.

Layout: the video list (tick the videos to work on), the Batch Edit Settings cards (one
switch per operation), Output Settings, the Processing Queue and the Batch Summary with
**Start Batch**.

Every ticked video becomes its own ``render_video`` job in the Export Queue: the same export
the Edit page uses (Smart Cut, captions, Resize / Crop or Auto Reframe, speed and audio in one
FFmpeg pass, NVENC when it works, else the CPU). At most "Max concurrent renders" (Settings,
default 1) run at once, so videos are processed one after another. A finished file is read
back with FFprobe before the job counts as completed. Videos use the shared settings unless
**Apply to Selected** gave them their own. The batch list is saved in the user database, so it
survives a restart or crash.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, QStandardPaths, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio.captions.models import installed_path
from ai_video_studio.captions.srt import read_srt_file
from ai_video_studio.core import hardware, paths
from ai_video_studio.core.errors import StudioError
from ai_video_studio.core.ffmpeg_tools import VideoMetadata
from ai_video_studio.core.media import format_duration, validate_video_path
from ai_video_studio.core.process import run_process
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
    preset_summary,
)
from ai_video_studio.render.layout import aspect_text
from ai_video_studio.render.naming import CONFLICT_POLICIES, path_key, plan_outputs
from ai_video_studio.storage.settings import REFRAME_DEFAULT_KEYS
from ai_video_studio.ui.caption_dialogs import FramePreviewDialog
from ai_video_studio.ui.project_controller import JobBridge, ProjectController, expand_paths
from ai_video_studio.ui.widgets.cards import TEAL
from ai_video_studio.ui.widgets.common import muted, open_folder, reveal_in_explorer, section_title
from ai_video_studio.ui.widgets.preset_editor import PresetDialog, PresetEditor
from ai_video_studio.ui.widgets.video_preview import VideoPreview

log = logging.getLogger(__name__)

COLUMNS = ["", "", "Video", "Duration", "Resolution", "Size", "Settings", "Status", "Preview", ""]
COL_CHECK, COL_THUMB, COL_VIDEO, COL_DURATION, COL_RESOLUTION, COL_SIZE, COL_SETTINGS, COL_STATUS, COL_PREVIEW, \
    COL_REMOVE = range(len(COLUMNS))
QUEUE_COLUMNS = ["#", "Video", "Output", "Export mode", "Progress", "Status", "Details", "Action"]
Q_OUTPUT, Q_MODE, Q_PROGRESS, Q_STATUS, Q_DETAILS, Q_ACTION = 2, 3, 4, 5, 6, 7
STATUS_LABELS = {
    "waiting": "Waiting", "held": "Waiting (paused)", "analyzing": "Processing", "ready": "Ready",
    "rendering": "Processing", "completed": "Completed", "failed": "Failed", "cancelled": "Cancelled",
    "skipped": "Skipped",
}
STATUS_COLORS = {"completed": "#5fd38d", "failed": "#ff6b6b", "cancelled": "#ffb84d", "skipped": "#ffb84d",
                 "analyzing": TEAL, "rendering": TEAL}
ACTIVE = {JobState.QUEUED, JobState.RUNNING, JobState.HELD}
THUMB = QSize(80, 45)
PAGE_STYLE = f"""
QFrame#Panel {{ background-color: #15181d; border: 1px solid #262b33; border-radius: 10px; }}
QPushButton#StartBatch {{ background-color: {TEAL}; border: 1px solid {TEAL}; color: #06201c; font-size: 13pt;
    font-weight: 700; padding: 10px; border-radius: 8px; }}
QPushButton#StartBatch:hover {{ background-color: #35d8c4; }}
QPushButton#StartBatch:disabled {{ background-color: #1f3b38; border-color: #1f3b38; color: #5c7f7a; }}
QPushButton#Teal {{ background-color: {TEAL}; border-color: {TEAL}; color: #06201c; font-weight: 600; }}
QPushButton#Teal:disabled {{ background-color: #1f3b38; border-color: #1f3b38; color: #5c7f7a; }}
"""


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def panel(title: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Panel")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(12, 10, 12, 10)
    if title:
        layout.addWidget(section_title(title))
    return frame, layout


class _ThumbSignals(QObject):
    ready = Signal(str, str)  # video path, image path


class _HardwareSignals(QObject):
    ready = Signal(object)  # core.hardware.HardwareInfo


class _HardwareJob(QRunnable):
    """Hardware and encoder detection off the GUI thread (a few short test encodes)."""

    def __init__(self, ffmpeg: Path | None, refresh: bool, signals: _HardwareSignals):
        super().__init__()
        self.ffmpeg, self.refresh, self.signals = ffmpeg, refresh, signals

    def run(self) -> None:
        try:
            info = hardware.detect(self.ffmpeg, refresh=self.refresh)
        except Exception:  # noqa: BLE001 - detection must never break the page
            log.exception("Hardware detection failed")
            info = None
        self.signals.ready.emit(info)


class _ThumbJob(QRunnable):
    """Grab one small frame with FFmpeg off the GUI thread (cached in the temp folder)."""

    def __init__(self, ffmpeg: Path, video: str, at: float, out: Path, signals: _ThumbSignals):
        super().__init__()
        self.ffmpeg, self.video, self.at, self.out, self.signals = ffmpeg, video, at, out, signals

    def run(self) -> None:
        try:
            if not self.out.is_file():
                self.out.parent.mkdir(parents=True, exist_ok=True)
                run_process([str(self.ffmpeg), "-hide_banner", "-nostdin", "-y", "-v", "error", "-ss", f"{self.at:.2f}",
                             "-i", self.video, "-frames:v", "1", "-vf", f"scale={THUMB.width() * 2}:-2",
                             str(self.out)], timeout=30)
            if self.out.is_file():
                self.signals.ready.emit(self.video, str(self.out))
        except Exception:  # noqa: BLE001 - a missing thumbnail must never break the page
            log.debug("Thumbnail failed", extra={"path": self.video})


class SourcePreviewDialog(QDialog):
    """Play a source video (the original, before editing)."""

    def __init__(self, parent: QWidget | None, path: str, fps: float | None):
        super().__init__(parent)
        self.setWindowTitle(f"Preview: {Path(path).name}")
        self.preview = VideoPreview()
        layout = QVBoxLayout(self)
        layout.addWidget(self.preview)
        layout.addWidget(muted("The original video. Use Preview to see one frame with the batch settings."))
        self.resize(820, 560)
        self.preview.load(path, fps)

    def done(self, result: int) -> None:  # noqa: D102 - release the file when the dialog closes
        self.preview.player.stop()
        self.preview.load(None)
        super().done(result)


def format_seconds(seconds: float) -> str:
    seconds = int(round(max(0.0, seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def seconds_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        start = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - start).total_seconds()


class LogDialog(QDialog):
    def __init__(self, parent: QWidget | None, name: str, text: str):
        super().__init__(parent)
        self.setWindowTitle(f"Export log: {name}")
        view = QPlainTextEdit(text)
        view.setReadOnly(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        logs = buttons.addButton("Open Logs Folder", QDialogButtonBox.ButtonRole.ActionRole)
        logs.clicked.connect(lambda: open_folder(str(paths.logs_dir())))
        layout = QVBoxLayout(self)
        layout.addWidget(view, 1)
        layout.addWidget(buttons)
        self.text = text
        self.resize(760, 460)


class BeforeAfterDialog(QDialog):
    """The same frame without and with the colour grade / video filter, side by side."""

    def __init__(self, parent: QWidget | None, before: str, after: str, name: str, label: str):
        super().__init__(parent)
        self.setWindowTitle(f"Before / After: {name}")
        row = QHBoxLayout()
        for title, image in (("Before (original colours)", before), (f"After ({label})", after)):
            column = QVBoxLayout()
            caption = QLabel(title)
            caption.setObjectName("CardTitle")
            picture = QLabel()
            picture.setPixmap(QPixmap(image).scaled(620, 620, Qt.AspectRatioMode.KeepAspectRatio,
                                                    Qt.TransformationMode.SmoothTransformation))
            column.addWidget(caption)
            column.addWidget(picture, 1)
            row.addLayout(column)
        layout = QVBoxLayout(self)
        layout.addWidget(muted("One frame rendered by FFmpeg with the batch framing and captions, without and with "
                               "the colour settings: the same filters the export uses. This is a preview; nothing "
                               "is exported."))
        layout.addLayout(row, 1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)


class BulkPage(QWidget):
    def __init__(self, controller: ProjectController, bridge: JobBridge):
        super().__init__()
        self.controller = controller
        self.services = controller.services
        self._probes: dict[str, str] = {}   # probe job id -> item id
        self._filling = False
        self._thumbs: dict[str, QPixmap] = {}
        self._thumb_pool = QThreadPool(self)
        self._thumb_pool.setMaxThreadCount(2)
        self._thumb_signals = _ThumbSignals()
        self._thumb_signals.ready.connect(self._on_thumbnail)
        self._starting = False
        self.hardware = None
        self._frame_cache: dict[str, str] = {}               # frame payload -> rendered image (Before / After)
        self._compare_jobs: dict[str, tuple[str, str]] = {}  # job id -> (side, cache key)
        self._preview_job: str | None = None
        self._preview_notes: list[str] = []
        self.batch = self.services.batches.latest() or Batch()
        if not self.batch.output_dir:
            self.batch.output_dir = default_output_dir()
        self.setAcceptDrops(True)
        self.setStyleSheet(PAGE_STYLE)

        # -- header --
        title = QLabel("Bulk Processing")
        title.setObjectName("PageTitle")
        subtitle = muted("Apply editing settings to multiple videos at once.")
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(220)
        self.apply_preset_button = QPushButton("Load Preset")
        self.save_preset_button = QPushButton("Save Preset")
        self.save_preset_button.setObjectName("Teal")
        self.preset_menu_button = QToolButton()
        self.preset_menu_button.setText("⋯")
        self.preset_menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self)
        self.update_preset_button = QAction("Update Preset with Current Settings", self)
        self.rename_preset_button = QAction("Rename Preset…", self)
        self.duplicate_preset_button = QAction("Duplicate Preset…", self)
        self.delete_preset_button = QAction("Delete Preset…", self)
        for action in (self.update_preset_button, self.rename_preset_button, self.duplicate_preset_button,
                       self.delete_preset_button):
            menu.addAction(action)
        self.preset_menu_button.setMenu(menu)
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.addWidget(title)
        titles.addWidget(subtitle)
        head.addLayout(titles, 1)
        head.addWidget(QLabel("Preset:"))
        for w in (self.preset_combo, self.apply_preset_button, self.save_preset_button, self.preset_menu_button):
            head.addWidget(w)

        # -- video list --
        self.add_button = QPushButton("＋ Add Videos")
        self.add_button.setObjectName("Teal")
        self.add_folder_button = QPushButton("Add Folder")
        self.remove_button = QPushButton("Remove Selected")
        self.clear_button = QPushButton("Clear All")
        self.select_all = QCheckBox("Select all")
        self.folder_button = QToolButton()
        self.folder_button.setText("Select Folder ▾")
        self.folder_button.setToolTip("Tick only the videos of one folder (then use Apply to Selected).")
        self.folder_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.folder_menu = QMenu(self)
        self.folder_menu.aboutToShow.connect(self._fill_folder_menu)
        self.folder_button.setMenu(self.folder_menu)
        self.selected_label = QLabel("")
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(THUMB.height() + 10)
        self.table.setIconSize(THUMB)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_VIDEO, QHeaderView.ResizeMode.Stretch)
        for col in (COL_DURATION, COL_RESOLUTION, COL_SIZE, COL_SETTINGS, COL_STATUS):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setWordWrap(False)
        for col, width in ((COL_CHECK, 28), (COL_THUMB, THUMB.width() + 8), (COL_PREVIEW, 60), (COL_REMOVE, 36)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(col, width)
        self.empty_label = muted("No videos yet. Click Add Videos, Add Folder, or drop video files here.")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        list_frame, list_layout = panel()
        tools = QHBoxLayout()
        for w in (self.add_button, self.add_folder_button, self.remove_button, self.clear_button):
            tools.addWidget(w)
        tools.addStretch(1)
        tools.addWidget(self.select_all)
        tools.addWidget(self.folder_button)
        tools.addWidget(self.selected_label)
        list_layout.addLayout(tools)
        list_layout.addWidget(self.table, 1)
        list_layout.addWidget(self.empty_label)

        # -- batch edit settings --
        self.editor = PresetEditor(self.batch.shared, caption_presets=self.services.presets,
                                   color_presets=self.services.color_presets)
        self.apply_selected_button = QPushButton("Apply to Selected")
        self.apply_selected_button.setToolTip("Give the ticked videos their own copy of these settings. The others "
                                              "keep following the shared settings.")
        self.shared_button = QPushButton("Reset Selected")
        self.shared_button.setToolTip("Reset Selected Video Settings: the ticked videos lose their own settings "
                                      "and follow the shared settings again.")
        self.all_shared_button = QPushButton("Apply to All")
        self.all_shared_button.setToolTip("Apply to All Videos: every video uses these shared settings (removes "
                                          "per-video settings).")
        self.edit_item_button = QPushButton("Edit Video…")
        self.edit_item_button.setToolTip("Edit the settings of the highlighted video only (it gets its own settings).")
        self.preview_button = QPushButton("Preview")
        self.preview_button.setToolTip("Render one frame of the highlighted (or first ticked) video with its settings "
                                       "(framing and caption style), without exporting the video.")
        self.settings_error = QLabel("")
        self.settings_error.setObjectName("Warning")
        self.settings_error.setWordWrap(True)
        settings_frame, settings_layout = panel()
        srow = QHBoxLayout()
        srow.addWidget(section_title("Batch Edit Settings"))
        srow.addStretch(1)
        for b in (self.preview_button, self.edit_item_button, self.apply_selected_button, self.shared_button,
                  self.all_shared_button):
            srow.addWidget(b)
        settings_layout.addLayout(srow)
        settings_layout.addWidget(muted("These are the shared settings: every video uses them unless Apply to Selected "
                                        "gave it its own (see the Settings column)."))
        settings_layout.addWidget(self.settings_error)
        scroll = QScrollArea()
        scroll.setWidget(self.editor)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        settings_layout.addWidget(scroll, 1)

        # -- output settings --
        self.output_dir = QLineEdit(self.batch.output_dir)
        self.output_dir.setPlaceholderText("Folder for the exported videos")
        self.browse_button = QPushButton("…")
        self.browse_button.setToolTip("Choose the output folder")
        self.conflict = QComboBox()
        for key, label in CONFLICT_POLICIES.items():
            self.conflict.addItem(label, key)
        self.conflict.setCurrentIndex(max(0, self.conflict.findData(self.batch.conflict)))
        out_frame, out_layout = panel("Output Settings")
        form = QFormLayout()
        orow = QHBoxLayout()
        orow.addWidget(self.output_dir, 1)
        orow.addWidget(self.browse_button)
        form.addRow("Output folder:", orow)
        form.addRow("File name:", self.editor.template)
        form.addRow("If the file exists:", self.conflict)
        self.resolved = QLabel()
        self.resolved.setObjectName("Resolved")
        self.resolved.setWordWrap(True)
        self.resolved.setTextFormat(Qt.TextFormat.RichText)
        self.resolved.setStyleSheet(f"QLabel#Resolved {{ border: 1px solid {TEAL}; border-radius: 8px; "
                                    "padding: 8px; }")
        self.hardware_label = muted("Hardware: not detected yet.")
        self.hardware_label.setWordWrap(True)
        self.detect_button = QPushButton("Detect Hardware")
        self.detect_button.setToolTip("Check the CPU, memory, GPUs and which FFmpeg encoders really work here "
                                      "(a short test encode each).")
        self.recommend_button = QPushButton("Use Recommended")
        self.recommend_button.setToolTip("Auto encoder and the recommended number of exports at the same time.")
        self.recommend_button.setEnabled(False)
        hw_row = QHBoxLayout()
        hw_row.addWidget(self.detect_button)
        hw_row.addWidget(self.recommend_button)
        out_layout.addLayout(form)
        out_layout.addWidget(section_title("Export preview"))
        out_layout.addWidget(self.resolved)
        out_layout.addWidget(self.editor.output)
        out_layout.addWidget(section_title("Hardware"))
        out_layout.addWidget(self.hardware_label)
        out_layout.addLayout(hw_row)
        out_layout.addStretch(1)
        out_scroll = QScrollArea()
        out_scroll.setWidget(out_frame)
        out_scroll.setWidgetResizable(True)
        out_scroll.setFrameShape(QFrame.Shape.NoFrame)
        out_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for box in self.editor.output.findChildren(QComboBox) + [self.conflict]:
            box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            box.setMinimumContentsLength(8)

        # -- processing queue --
        self.queue_table = QTableWidget(0, len(QUEUE_COLUMNS))
        self.queue_table.setHorizontalHeaderLabels(QUEUE_COLUMNS)
        self.queue_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.queue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.queue_table.verticalHeader().setVisible(False)
        qh = self.queue_table.horizontalHeader()
        qh.setSectionResizeMode(Q_DETAILS, QHeaderView.ResizeMode.Stretch)
        self.queue_table.setColumnWidth(0, 32)
        self.queue_table.setColumnWidth(1, 170)
        self.queue_table.setColumnWidth(Q_OUTPUT, 120)
        self.queue_table.setColumnWidth(Q_MODE, 120)
        self.queue_table.setColumnWidth(Q_PROGRESS, 110)
        self.clear_completed_button = QPushButton("Clear Completed")
        self.clear_completed_button.setToolTip("Remove finished videos from the list. Their exported files are kept.")
        self.retry_button = QPushButton("Retry Failed")
        self.pause_button = QPushButton("Pause Batch")
        self.resume_button = QPushButton("Resume Batch")
        self.cancel_button = QPushButton("Cancel Selected")
        self.cancel_button.setToolTip("Cancel the videos highlighted in the queue (or the ticked videos).")
        self.cancel_all_button = QPushButton("Cancel Batch")
        self.cancel_all_button.setToolTip("Cancel the running export and every video still waiting.")
        self.log_button = QPushButton("View Log")
        self.log_button.setToolTip("Details of the highlighted video's export: steps and their times, encoder, "
                                   "warnings and errors.")
        self.open_button = QPushButton("Open Output Folder")
        queue_frame, queue_layout = panel("Processing Queue")
        queue_layout.addWidget(self.queue_table, 1)
        qrow = QHBoxLayout()
        for b in (self.clear_completed_button, self.retry_button, self.pause_button, self.resume_button,
                  self.cancel_button, self.cancel_all_button, self.log_button, self.open_button):
            qrow.addWidget(b)
        queue_layout.addLayout(qrow)

        # -- summary and start --
        self.summary = muted("")
        self.summary.setWordWrap(True)
        self.counts = {k: QLabel("0") for k in ("Total videos", "Selected", "Completed", "Processing", "Waiting",
                                                 "Failed", "Total duration", "Output size", "Elapsed",
                                                 "Remaining (estimate)", "Current step")}
        self.overall = QProgressBar()
        self.overall.setRange(0, 100)
        self.start_button = QPushButton("▶  Start Batch")
        self.start_button.setObjectName("StartBatch")
        self.start_button.setToolTip("Export every ticked, ready video with its settings, one after another.")
        sum_frame, sum_layout = panel("Batch Summary")
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        for n, (name, label) in enumerate(self.counts.items()):
            label.setAlignment(Qt.AlignmentFlag.AlignRight)
            grid.addWidget(muted(name), n, 0)
            grid.addWidget(label, n, 1)
        sum_layout.addLayout(grid)
        sum_layout.addWidget(self.overall)
        sum_layout.addWidget(self.summary)
        sum_layout.addStretch(1)
        sum_layout.addWidget(self.start_button)

        top = QSplitter()
        top.addWidget(list_frame)
        top.addWidget(settings_frame)
        top.setStretchFactor(0, 1)
        top.setStretchFactor(1, 1)
        top.setSizes([900, 1000])
        bottom = QSplitter()
        bottom.addWidget(out_scroll)
        bottom.addWidget(queue_frame)
        bottom.addWidget(sum_frame)
        bottom.setStretchFactor(0, 3)
        bottom.setStretchFactor(1, 4)
        bottom.setStretchFactor(2, 2)
        bottom.setSizes([460, 900, 300])
        sum_frame.setMinimumWidth(280)
        out_scroll.setMinimumWidth(380)
        main = QSplitter(Qt.Orientation.Vertical)
        main.addWidget(top)
        main.addWidget(bottom)
        main.setStretchFactor(0, 3)
        main.setStretchFactor(1, 2)
        layout = QVBoxLayout(self)
        layout.addLayout(head)
        layout.addWidget(main, 1)

        # -- connections --
        self.add_button.clicked.connect(self.add_dialog)
        self.add_folder_button.clicked.connect(self.add_folder_dialog)
        self.remove_button.clicked.connect(self.remove_selected)
        self.clear_button.clicked.connect(self.clear_list)
        self.select_all.clicked.connect(self._on_select_all)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.itemSelectionChanged.connect(self._update_resolved)
        self.queue_table.itemSelectionChanged.connect(self._update_buttons)
        self.apply_selected_button.clicked.connect(self.apply_to_selected)
        self.shared_button.clicked.connect(self.use_shared_for_selected)
        self.all_shared_button.clicked.connect(self.apply_shared_to_all)
        self.preview_button.clicked.connect(self.preview_selected)
        self.edit_item_button.clicked.connect(self.edit_item_settings)
        self.editor.compare_button.clicked.connect(self.compare_colors)
        self.table.cellDoubleClicked.connect(lambda row, _col: self.edit_item_settings())
        self.apply_preset_button.clicked.connect(self.apply_preset)
        self.save_preset_button.clicked.connect(self.save_preset)
        self.update_preset_button.triggered.connect(self.update_preset)
        self.rename_preset_button.triggered.connect(self.rename_preset)
        self.delete_preset_button.triggered.connect(self.delete_preset)
        self.preset_combo.currentIndexChanged.connect(self._update_buttons)
        self.editor.changed.connect(self._on_shared_changed)
        self.output_dir.editingFinished.connect(self._on_output_changed)
        self.browse_button.clicked.connect(self._browse_output)
        self.conflict.currentIndexChanged.connect(self._on_output_changed)
        self.start_button.clicked.connect(self.start)
        self.cancel_all_button.clicked.connect(self.cancel_batch)
        self.log_button.clicked.connect(self.show_log)
        self.detect_button.clicked.connect(lambda: self.detect_hardware(refresh=True))
        self.recommend_button.clicked.connect(self.use_recommended)
        self.duplicate_preset_button.triggered.connect(self.duplicate_preset)
        self._hw_signals = _HardwareSignals()
        self._hw_signals.ready.connect(self._on_hardware)
        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._tick)
        self._clock.start()
        self.pause_button.clicked.connect(self.pause)
        self.resume_button.clicked.connect(self.resume)
        self.cancel_button.clicked.connect(self.cancel_selected)
        self.retry_button.clicked.connect(self.retry_failed)
        self.clear_completed_button.clicked.connect(self.clear_completed)
        self.open_button.clicked.connect(lambda: self.batch.output_dir and open_folder(self.batch.output_dir))
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
            message += f", {len(rejected)} skipped: " + "; ".join(rejected[:3]) + ("…" if len(rejected) > 3 else "")
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
        """The ticked videos."""
        return [i for i in self.batch.items if i.selected]

    def highlighted_items(self) -> list[BatchItem]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        items = []
        for r in rows:
            cell = self.table.item(r, COL_VIDEO)
            item = self.batch.find(cell.data(Qt.ItemDataRole.UserRole)) if cell else None
            if item is not None:
                items.append(item)
        return items

    def _active(self, item: BatchItem) -> bool:
        job = self.job(item)
        return job is not None and job.state in ACTIVE

    def remove_items(self, items: list[BatchItem]) -> None:
        busy = [i.name for i in items if self._active(i)]
        if busy:
            QMessageBox.warning(self, "Remove", "Cancel these first, they are queued or rendering:\n" + "\n".join(busy))
            return
        self.batch.remove({i.id for i in items})
        self.save()
        self.refresh()

    def remove_selected(self) -> None:
        self.remove_items(self.selected_items())

    def clear_list(self) -> None:
        if any(self._active(i) for i in self.batch.items):
            QMessageBox.warning(self, "Clear All", "Some videos are still queued or rendering. Cancel them first.")
            return
        if self.batch.items and QMessageBox.question(self, "Clear All", "Remove all videos from the list? "
                                                     "Exported files are kept.") != QMessageBox.StandardButton.Yes:
            return
        old = self.batch
        self.services.batches.delete(old.id)
        self.batch = Batch(shared=old.shared, preset_name=old.preset_name, output_dir=old.output_dir,
                           conflict=old.conflict)
        self.save()
        self.refresh()

    def clear_completed(self) -> None:
        """Remove finished videos from the list; their exported files stay where they are."""
        done = [i for i in self.batch.items if self.status(i)[0] == "completed"]
        self.batch.remove({i.id for i in done})
        self.save()
        self.refresh()
        self.summary.setText(f"{len(done)} completed video(s) removed from the list; the exported files are kept.")

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        dropped = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if dropped:
            self.add_paths(dropped)
        event.acceptProposedAction()

    # ------------------------------------------------------------------ ticking videos
    def _on_item_changed(self, cell: QTableWidgetItem) -> None:
        if self._filling or cell.column() != COL_CHECK:
            return
        item = self.batch.find(cell.data(Qt.ItemDataRole.UserRole))
        if item is not None:
            item.selected = cell.checkState() == Qt.CheckState.Checked
            self.save()
            self._update_summary()
            self._update_buttons()

    def set_selected(self, items: list[BatchItem], selected: bool) -> None:
        for item in items:
            item.selected = selected
        self.save()
        self.refresh()

    def _on_select_all(self, checked: bool) -> None:
        self.set_selected(self.batch.items, checked)

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
        try:
            preset = self.services.render_presets.get(name)
        except StudioError as exc:
            QMessageBox.warning(self, "Load Preset", str(exc))
            return
        self.editor.set_preset(preset)
        self.batch.preset_name = name
        self._on_shared_changed()
        self.summary.setText(f"Preset '{name}' loaded as the shared settings.")

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
        name, ok = QInputDialog.getText(self, "Save Preset", "Preset name:")
        if not ok or not name.strip():
            return
        store = self.services.render_presets
        if store.exists(name.strip()) and QMessageBox.question(
                self, "Save Preset", f"Replace the preset '{name.strip()}'?") != QMessageBox.StandardButton.Yes:
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

    def duplicate_preset(self) -> str | None:
        name = self.selected_preset()
        if not name:
            return None
        new, ok = QInputDialog.getText(self, "Duplicate Preset", "Name of the copy:", text=f"{name} copy")
        if not ok:
            return None
        try:
            new = self.services.render_presets.save(new, self.services.render_presets.get(name))
        except StudioError as exc:
            QMessageBox.warning(self, "Duplicate Preset", str(exc))
            return None
        self._fill_presets(new)
        return new

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
        self._update_resolved()
        try:
            self.batch.shared = self.editor.preset()
            self.settings_error.setText("" if self.editor.enabled_operations() else
                                        "No editing operation is switched on: the videos are only re-encoded.")
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

    def apply_to_selected(self) -> None:
        """Give the ticked videos (not yet started) their own copy of the current settings."""
        try:
            preset = self.editor.preset()
        except StudioError as exc:
            QMessageBox.warning(self, "Apply to Selected", str(exc))
            return
        targets = [i for i in self.selected_items() if not self._active(i)]
        for item in targets:
            item.override = preset.copy()
        self.save()
        self.refresh()
        ops = ", ".join(self.editor.enabled_operations()) or "no editing (re-encode only)"
        self.summary.setText(f"Settings applied to {len(targets)} video(s): {ops}. Nothing starts until you click "
                             "Start Batch.")

    def use_shared_for_selected(self) -> None:
        for item in self.selected_items():
            item.override = None
        self.save()
        self.refresh()

    def apply_shared_to_all(self, confirm: bool = True) -> None:
        custom = [i for i in self.batch.items if i.override is not None]
        if custom and confirm and QMessageBox.question(
                self, "Apply to All Videos", f"{len(custom)} video(s) have their own settings. Use these "
                "shared settings for every video?") != QMessageBox.StandardButton.Yes:
            return
        for item in custom:
            item.override = None
        self.save()
        self.refresh()
        self.summary.setText(f"All {len(self.batch.items)} video(s) use the shared settings.")

    def edit_item_settings(self, item: BatchItem | None = None) -> bool:
        """Edit one video's own settings in a dialog (the other videos are not changed)."""
        item = item or next(iter(self.highlighted_items()), None)
        if item is None:
            self.summary.setText("Highlight a video in the list to edit its settings.")
            return False
        if self._active(item):
            QMessageBox.information(self, "Edit Video Settings", f"{item.name} is being processed; its settings "
                                    "cannot be changed now.")
            return False
        dialog = PresetDialog(self, f"Settings for {item.name}", self.batch.effective(item).copy(),
                              caption_presets=self.services.presets, color_presets=self.services.color_presets)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        item.override = dialog.result_preset
        self.save()
        self.refresh()
        self.summary.setText(f"{item.name} now has its own settings: "
                             + (", ".join(preset_summary(item.override)) or "re-encode only") + ".")
        return True

    def frame_payloads_for_compare(self, item: BatchItem) -> tuple[dict, dict, list[str]]:
        """Frame payloads without and with the colour settings (the same frame, framing and captions)."""
        preset = self.batch.effective(item)
        captions, style = self.captions_for(item)
        after, notes = frame_preview_payload(item, preset, captions, style)
        before = dict(after, color=None)
        return before, after, notes

    def compare_colors(self) -> list[Job]:
        """Before / After: render one frame without and with the colour grade and filter (cached)."""
        items = [i for i in (self.highlighted_items() or self.selected_items() or self.batch.items)
                 if i.metadata is not None]
        if not items:
            self.summary.setText("Add a video first: the comparison uses one of its frames.")
            return []
        item = items[0]
        try:
            before, after, _notes = self.frame_payloads_for_compare(item)
        except StudioError as exc:
            QMessageBox.warning(self, "Before / After", str(exc))
            return []
        if not after.get("color"):
            self.summary.setText("Switch on Color Grading or a Video Filter (above 0%) to compare.")
            return []
        self._compare = {"item": item.name, "before": None, "after": None,
                         "label": ", ".join(self.batch.effective(item).color.summary())}
        jobs = []
        for side, payload in (("before", before), ("after", after)):
            key = json.dumps(payload, sort_keys=True)
            cached = self._frame_cache.get(key)
            if cached and Path(cached).is_file():
                self._compare[side] = cached
                continue
            job = self.services.jobs.submit(RENDER_FRAME_JOB, f"Colour comparison ({side}): {item.name}", payload)
            self._compare_jobs[job.id] = (side, key)
            jobs.append(job)
        self.summary.setText(f"Rendering the Before / After frames of {item.name}…" if jobs else "")
        self._show_compare()
        return jobs

    def _show_compare(self) -> None:
        c = getattr(self, "_compare", None)
        if not c or not c["before"] or not c["after"]:
            return
        self._compare = None
        self.summary.setText("")
        BeforeAfterDialog(self, c["before"], c["after"], c["item"], c["label"]).exec()

    def preview_selected(self) -> Job | None:
        items = self.highlighted_items() or self.selected_items()
        items = [i for i in items if i.metadata is not None]
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

    def play_source(self, item: BatchItem) -> None:
        SourcePreviewDialog(self, item.path, item.metadata.fps if item.metadata else None).exec()

    # ------------------------------------------------------------------ folders
    def folders(self) -> list[str]:
        return sorted({str(Path(i.path).parent) for i in self.batch.items}, key=str.casefold)

    def _fill_folder_menu(self) -> None:
        self.folder_menu.clear()
        for folder in self.folders():
            count = sum(str(Path(i.path).parent) == folder for i in self.batch.items)
            action = self.folder_menu.addAction(f"{folder}  ({count})")
            action.triggered.connect(lambda _c=False, f=folder: self.select_folder(f))

    def select_folder(self, folder: str) -> None:
        """Tick exactly the videos in ``folder``."""
        for item in self.batch.items:
            item.selected = str(Path(item.path).parent) == folder
        self.save()
        self.refresh()

    # ------------------------------------------------------------------ hardware
    def detect_hardware(self, refresh: bool = False) -> None:
        try:
            ffmpeg = self.services.ffmpeg_path()
        except StudioError:
            ffmpeg = None
        known = hardware.cached(ffmpeg) if not refresh else None
        if known is not None:
            self._on_hardware(known)
            return
        self.detect_button.setEnabled(False)
        self.hardware_label.setText("Detecting hardware and testing encoders…")
        self._thumb_pool.start(_HardwareJob(ffmpeg, refresh, self._hw_signals))

    def _on_hardware(self, info) -> None:
        self.detect_button.setEnabled(True)
        self.hardware = info
        if info is None:
            self.hardware_label.setText("⚠ Hardware detection failed (see the log). Exports still work: the "
                                        "encoder is tested when an export starts.")
            return
        rec = hardware.recommend(info)
        self._recommendation = rec
        self.hardware_label.setText("\n".join(info.summary_lines() + [
            f"Recommended: Auto encoder ({rec.encoder_label}), {rec.max_renders} export(s) at a time."] + rec.notes))
        self.recommend_button.setEnabled(True)
        self.editor.output.set_hardware(info)
        self._update_resolved()

    def use_recommended(self) -> None:
        rec = getattr(self, "_recommendation", None)
        if rec is None:
            return
        settings = self.editor.output.settings()
        settings.encoder = "auto"
        self.editor.output.set_settings(settings)
        self.services.settings.set("max_concurrent_renders", rec.max_renders)
        self.summary.setText(f"Encoder: Auto ({rec.encoder_label}); {rec.max_renders} export(s) at a time. AI "
                             "analysis (Whisper, face tracking) still runs one at a time.")
        self._on_shared_changed()

    def resolved_encoder(self, preset) -> tuple[str, str] | None:
        """``(encoder label, processing mode)`` the export will use, once hardware was detected."""
        info = getattr(self, "hardware", None)
        if info is None:
            return None
        from ai_video_studio.core.ffmpeg_commands import EncoderUnavailableError, choose_video_encoder, encoder_family
        from ai_video_studio.core.ffmpeg_commands import encoder_label as label_of

        try:
            codec, _reason = choose_video_encoder(preset.output.encoder, self.services.ffmpeg_path(),
                                                  preset.output.codec, preset.output.allow_fallback)
        except (EncoderUnavailableError, StudioError) as exc:
            return f"⚠ {exc}", "—"
        gpu = encoder_family(codec) != "cpu"
        return label_of(codec), ("GPU-accelerated encoding" if gpu else "CPU encoding") + (
            " · GPU decoding when possible" if preset.output.hw_decode == "auto" else "")

    def _update_resolved(self) -> None:
        """What will be rendered, from the same values the export uses."""
        from ai_video_studio.render.layout import RESOLUTIONS, aspect_and_resolution

        try:
            preset = self.editor.preset()
        except StudioError as exc:
            self.resolved.setText(f"⚠ {exc}")
            return
        item = next(iter(self.highlighted_items() or self.selected_items() or self.batch.items), None)
        meta = item.metadata if item is not None else None
        rows = []
        if preset.operation == "none":
            size, aspect, res = ("each video's own size" if meta is None else
                                 f"{meta.display_width} × {meta.display_height}"), "original", "Source"
        else:
            aspect_key, short = aspect_and_resolution(preset.resize)
            src = (meta.display_width, meta.display_height) if meta else (1920, 1080)
            w, h = output_size(preset, meta) if meta else preset.resize.output_size(*src)
            size = f"{w} × {h}"
            aspect = aspect_text(w, h) if aspect_key != "original" else "original"
            res = RESOLUTIONS.get(short, "Custom") if aspect_key != "custom" else "Custom"
            if meta and (w > meta.display_width * 1.01 and h > meta.display_height * 1.01):
                rows.append("⚠ Larger than the source: the video is upscaled (no extra detail).")
        rows = [f"<b>Aspect ratio:</b> {aspect}", f"<b>Output resolution:</b> {size}",
                f"<b>Resolution preset:</b> {res}", f"<b>Quality:</b> {preset.output.describe()}"] + rows
        encoder = self.resolved_encoder(preset)
        rows += ([f"<b>Encoder:</b> {encoder[0]}", f"<b>Processing mode:</b> {encoder[1]}"] if encoder else
                 ["<b>Encoder:</b> click Detect Hardware to see which encoder will be used"])
        if item is not None and meta is not None:
            try:
                name = output_name(preset, item, 1, self.batch.preset_name)
                rows.append(f"<b>File name:</b> {name}" + (" (e.g. for the first video)" if not
                                                           self.highlighted_items() else ""))
            except StudioError:
                pass
        if preset.output.mode == "draft":
            rows.append("⚠ Draft is for checking framing, captions and cuts, not for final delivery.")
        elif preset.output.mode in ("fast",):
            rows.append("Faster encoding gives larger files or slightly lower quality than Standard.")
        self.resolved.setText("<br>".join(rows))

    # ------------------------------------------------------------------ progress details
    def _tick(self) -> None:
        if any(self.status(i)[0] in ("analyzing", "rendering") for i in self.batch.items if i.job_id):
            self._fill_queue()
            self._update_summary()

    def cancel_batch(self) -> None:
        targets = [i for i in self.batch.items if self.status(i)[0] in ("waiting", "held", "analyzing", "rendering")]
        if targets and QMessageBox.question(self, "Cancel Batch", f"Cancel {len(targets)} video(s) that are "
                                            "waiting or being exported? Finished videos are kept.") == \
                QMessageBox.StandardButton.Yes:
            self.cancel_items(targets)

    def job_log(self, item: BatchItem) -> str:
        """A readable record of one video's export (for View Log)."""
        job = self.job(item)
        lines = [f"Video: {item.path}", f"Output: {item.output or '(not set yet)'}"]
        preset = self.batch.effective(item)
        lines.append("Settings: " + (", ".join(preset_summary(preset)) or "re-encode only") +
                     f"; {preset.output.describe()}")
        if job is None:
            lines.append("Not started.")
            return "\n".join(lines)
        lines.append(f"State: {job.state.value}" + (f" ({job.stage})" if job.stage else ""))
        lines += [f"Queued: {job.created_at}", f"Started: {job.started_at or '-'}",
                  f"Finished: {job.finished_at or '-'}"]
        live = self.services.jobs.detail(job.id)
        if live:
            lines.append("Now: " + ", ".join(f"{k}: {v}" for k, v in live.items() if v is not None))
        result = job.result or {}
        if result.get("encoder_label"):
            lines.append(f"Encoder used: {result['encoder_label']}")
        if result.get("timings"):
            lines.append("Time per step: " + ", ".join(f"{k.replace('_', ' ')} {format_seconds(v)}"
                                                         for k, v in result["timings"].items()))
        for warning in result.get("warnings") or []:
            lines.append(f"Note: {warning}")
        if job.error:
            lines.append(f"Error: {job.error}")
        lines.append(f"Application log folder: {paths.logs_dir()}")
        return "\n".join(lines)

    def show_log(self) -> None:
        item = next(iter(self.queue_items() or self.highlighted_items()), None)
        if item is None:
            self.summary.setText("Highlight a video in the queue to see its log.")
            return
        LogDialog(self, item.name, self.job_log(item)).exec()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.editor.fill_caption_styles()  # caption presets may have changed on the Captions page
        if getattr(self, "hardware", None) is None and self.detect_button.isEnabled():
            self.detect_hardware()

    # ------------------------------------------------------------------ thumbnails
    def _request_thumbnail(self, item: BatchItem) -> None:
        if item.path in self._thumbs or item.metadata is None:
            return
        try:
            stat = Path(item.path).stat()
            ffmpeg = self.services.ffmpeg_path()
        except (OSError, StudioError):
            return
        key = hashlib.sha1(f"{item.path}|{stat.st_size}|{stat.st_mtime}".encode()).hexdigest()[:20]
        out = paths.temp_dir() / "thumbnails" / f"{key}.jpg"
        self._thumbs[item.path] = QPixmap()  # requested
        if out.is_file():
            self._on_thumbnail(item.path, str(out))
            return
        at = min(max(0.0, item.metadata.duration * 0.1), 5.0)
        self._thumb_pool.start(_ThumbJob(ffmpeg, item.path, at, out, self._thumb_signals))

    def _on_thumbnail(self, video: str, image: str) -> None:
        pixmap = QPixmap(image)
        if pixmap.isNull():
            return
        self._thumbs[video] = pixmap.scaled(THUMB, Qt.AspectRatioMode.KeepAspectRatio,
                                            Qt.TransformationMode.SmoothTransformation)
        for row in range(self.table.rowCount()):
            cell = self.table.item(row, COL_VIDEO)
            item = self.batch.find(cell.data(Qt.ItemDataRole.UserRole)) if cell else None
            if item is not None and item.path == video:
                self.table.item(row, COL_THUMB).setIcon(QIcon(self._thumbs[video]))

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

    def status_text(self, item: BatchItem) -> str:
        status, _p = self.status(item)
        if status == "analyzing" and self.job(item) is None:
            return "Reading…"
        return STATUS_LABELS[status]

    def details(self, item: BatchItem) -> str:
        job = self.job(item)
        if job is not None and job.state is JobState.FAILED:
            return job.error or item.note
        if job is not None and job.state is JobState.COMPLETED:
            result = job.result or {}
            text = f"Saved: {item.output}" if item.output else ""
            took = sum((result.get("timings") or {}).values())
            if took:
                text += f"  ·  done in {format_seconds(took)}"
            if result.get("encoder_label"):
                text += f" with {result['encoder_label']}"
            if result.get("warnings"):
                text += ("  ·  " if text else "") + " ".join(result["warnings"])
            return text
        if job is not None and job.state is JobState.RUNNING:
            return self.live_text(job)
        return item.note or item.error or ""

    def live_text(self, job: Job) -> str:
        """What a running export is doing: step, encoder, FFmpeg speed, elapsed and an estimate."""
        live = self.services.jobs.detail(job.id)
        parts = [live.get("step") or ("Analysing…" if job.stage == "analyzing" else "Encoding…")]
        if job.stage != "analyzing" and live.get("encoder"):
            parts.append(live["encoder"])
        if job.stage != "analyzing" and live.get("speed"):
            parts.append(f"{live['speed']:.2f}× realtime")
        elapsed = seconds_since(job.started_at)
        if elapsed is not None:
            parts.append(f"elapsed {format_seconds(elapsed)}")
            remaining = self.remaining_estimate(job, elapsed)
            if remaining is not None:
                parts.append(f"about {format_seconds(remaining)} left")
        return "  ·  ".join(parts)

    @staticmethod
    def remaining_estimate(job: Job, elapsed: float) -> float | None:
        """From the job's real progress so far; none until there is enough of it to be meaningful."""
        if job.progress < 0.05 or elapsed < 10:
            return None
        return elapsed * (1 - job.progress) / job.progress

    def refresh(self) -> None:
        self._filling = True
        try:
            highlighted = {i.id for i in self.highlighted_items()}
            self.table.setRowCount(0)
            for item in self.batch.items:
                row = self.table.rowCount()
                self.table.insertRow(row)
                self._fill_row(row, item)
                if item.id in highlighted:
                    self.table.selectRow(row)
            self.table.setVisible(bool(self.batch.items))
            self.empty_label.setVisible(not self.batch.items)
            self.select_all.setChecked(bool(self.batch.items) and all(i.selected for i in self.batch.items))
            self._fill_queue()
        finally:
            self._filling = False
        self._update_summary()
        self._update_buttons()

    def _fill_row(self, row: int, item: BatchItem) -> None:
        meta = item.metadata
        status = self.status(item)[0]
        check = QTableWidgetItem()
        check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        check.setCheckState(Qt.CheckState.Checked if item.selected else Qt.CheckState.Unchecked)
        check.setData(Qt.ItemDataRole.UserRole, item.id)
        self.table.setItem(row, COL_CHECK, check)
        thumb = QTableWidgetItem()
        if self._thumbs.get(item.path) is not None and not self._thumbs[item.path].isNull():
            thumb.setIcon(QIcon(self._thumbs[item.path]))
        self.table.setItem(row, COL_THUMB, thumb)
        folder = str(Path(item.path).parent)
        name = QTableWidgetItem(f"{item.name}\n{folder if len(folder) < 60 else '…' + folder[-58:]}")
        name.setData(Qt.ItemDataRole.UserRole, item.id)
        name.setToolTip(item.path)
        self.table.setItem(row, COL_VIDEO, name)
        try:
            file_size = human_size(Path(item.path).stat().st_size)
        except OSError:
            file_size = "missing"
        preset = self.batch.effective(item)
        size = "-"
        if meta is not None:
            try:
                w, h = output_size(preset, meta)
                size = f"{meta.display_width}×{meta.display_height}" + (
                    f" → {w}×{h}" if (w, h) != (meta.display_width, meta.display_height) else "")
            except StudioError:
                size = meta.resolution_text
        values = {COL_DURATION: format_duration(meta.duration) if meta else "-", COL_RESOLUTION: size,
                  COL_SIZE: file_size, COL_SETTINGS: "Own" if item.override else "Shared",
                  COL_STATUS: self.status_text(item)}
        for col, value in values.items():
            cell = QTableWidgetItem(value)
            if col == COL_SETTINGS:
                cell.setToolTip(("This video has its own settings." if item.override else
                                 "This video follows the shared settings.") + "\n" +
                                ("\n".join(preset_summary(preset)) or "No editing: re-encode only."))
            if col == COL_STATUS:
                cell.setToolTip(self.details(item))
                if status in STATUS_COLORS:
                    cell.setForeground(QColor(STATUS_COLORS[status]))
            self.table.setItem(row, col, cell)
        play = QToolButton()
        play.setText("▶")
        play.setToolTip("Play the original video")
        play.setEnabled(Path(item.path).is_file())
        play.clicked.connect(lambda _c=False, i=item.id: self.batch.find(i) and self.play_source(self.batch.find(i)))
        self.table.setCellWidget(row, COL_PREVIEW, play)
        remove = QToolButton()
        remove.setText("✕")
        remove.setToolTip("Remove from the list (the file is not deleted)")
        remove.clicked.connect(lambda _c=False, i=item.id: self.batch.find(i) and self.remove_items([self.batch.find(i)]))
        self.table.setCellWidget(row, COL_REMOVE, remove)
        self._request_thumbnail(item)

    def _fill_queue(self) -> None:
        started = [i for i in self.batch.items if i.job_id or i.skipped]
        self.queue_table.setRowCount(0)
        for n, item in enumerate(started, 1):
            row = self.queue_table.rowCount()
            self.queue_table.insertRow(row)
            status, progress = self.status(item)
            number = QTableWidgetItem(str(n))
            number.setData(Qt.ItemDataRole.UserRole, item.id)
            self.queue_table.setItem(row, 0, number)
            name = QTableWidgetItem(item.name)
            name.setToolTip(item.path)
            self.queue_table.setItem(row, 1, name)
            preset = self.batch.effective(item)
            out = "-"
            if item.metadata is not None:
                try:
                    w, h = output_size(preset, item.metadata)
                    out = f"{w}×{h} ({aspect_text(w, h)})"
                except StudioError:
                    pass
            out_cell = QTableWidgetItem(out)
            out_cell.setToolTip(item.output or "")
            self.queue_table.setItem(row, Q_OUTPUT, out_cell)
            job = self.job(item)
            mode = preset.output.describe()
            if job is not None and job.result and job.result.get("encoder_label"):
                mode += f" · {job.result['encoder_label']}"
            mode_cell = QTableWidgetItem(mode)
            mode_cell.setToolTip("Enabled: " + (", ".join(preset_summary(preset)) or "re-encode only"))
            self.queue_table.setItem(row, Q_MODE, mode_cell)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(int(progress * 100))
            self.queue_table.setCellWidget(row, Q_PROGRESS, bar)
            state = QTableWidgetItem(STATUS_LABELS[status])
            if status in STATUS_COLORS:
                state.setForeground(QColor(STATUS_COLORS[status]))
            self.queue_table.setItem(row, Q_STATUS, state)
            detail = QTableWidgetItem(self.details(item))
            detail.setToolTip(self.details(item))
            self.queue_table.setItem(row, Q_DETAILS, detail)
            action = QToolButton()
            if status == "completed" and item.output:
                action.setText("Show File")
                action.clicked.connect(lambda _c=False, p=item.output: reveal_in_explorer(p))
            elif status in ("failed", "cancelled") and self.job(item) is not None:
                action.setText("Retry")
                action.clicked.connect(lambda _c=False, i=item.id: self.retry_items([self.batch.find(i)]))
            elif status in ("waiting", "held", "analyzing", "rendering"):
                action.setText("Cancel")
                action.clicked.connect(lambda _c=False, i=item.id: self.cancel_items([self.batch.find(i)]))
            else:
                action.setEnabled(False)
                action.setText("—")
            self.queue_table.setCellWidget(row, Q_ACTION, action)

    def queue_items(self) -> list[BatchItem]:
        rows = sorted({i.row() for i in self.queue_table.selectionModel().selectedRows()})
        return [self.batch.find(self.queue_table.item(r, 0).data(Qt.ItemDataRole.UserRole)) for r in rows
                if self.queue_table.item(r, 0)]

    def _update_summary(self) -> None:
        statuses = [self.status(i) for i in self.batch.items]
        counts: dict[str, int] = {}
        for status, _p in statuses:
            counts[status] = counts.get(status, 0) + 1
        selected = self.selected_items()
        self.selected_label.setText(f"{len(selected)} of {len(self.batch.items)} videos selected")
        out_bytes = 0
        for item, (status, _p) in zip(self.batch.items, statuses):
            if status == "completed" and item.output:
                try:
                    out_bytes += Path(item.output).stat().st_size
                except OSError:
                    pass
        duration = sum(i.metadata.duration for i in selected if i.metadata)
        values = {"Total videos": len(self.batch.items), "Selected": len(selected),
                  "Completed": counts.get("completed", 0),
                  "Processing": counts.get("rendering", 0) + sum(1 for i, (s, _p) in zip(self.batch.items, statuses)
                                                                 if s == "analyzing" and self.job(i) is not None),
                  "Waiting": counts.get("waiting", 0) + counts.get("held", 0),
                  "Failed": counts.get("failed", 0) + counts.get("cancelled", 0),
                  "Total duration": format_duration(duration), "Output size": human_size(out_bytes)}
        started = [s for i, s in zip(self.batch.items, statuses) if i.job_id or i.skipped]
        overall = item_progress(started)
        jobs = [self.job(i) for i in self.batch.items if i.job_id]
        times = [seconds_since(j.started_at) for j in jobs if j is not None and j.started_at]
        elapsed = max((t for t in times if t is not None), default=None)
        running = [j for j in jobs if j is not None and j.state is JobState.RUNNING]
        values["Elapsed"] = format_seconds(elapsed) if elapsed is not None and (running or overall < 1) else "-"
        values["Remaining (estimate)"] = (format_seconds(elapsed * (1 - overall) / overall)
                                          if running and elapsed and overall >= 0.05 else "-")
        values["Current step"] = self.live_text(running[0]).split("  ·  ")[0] if running else "-"
        for name, value in values.items():
            self.counts[name].setText(str(value))
        self.overall.setValue(int(overall * 100))

    def _update_buttons(self) -> None:
        selected = self.selected_items()
        jobs = [self.job(i) for i in self.batch.items]
        states = {j.state for j in jobs if j is not None}
        builtin = bool(self.selected_preset()) and dict(self.services.render_presets.names()).get(
            self.selected_preset(), True)
        ready_ticked = [i for i in selected if self.status(i)[0] == "ready"]
        self.remove_button.setEnabled(bool(selected))
        self.clear_button.setEnabled(bool(self.batch.items))
        self.apply_selected_button.setEnabled(bool(selected))
        self.shared_button.setEnabled(any(i.override for i in selected))
        self.all_shared_button.setEnabled(any(i.override for i in self.batch.items))
        self.preview_button.setEnabled(any(i.metadata is not None for i in (self.highlighted_items() or selected))
                                       and self._preview_job is None)
        self.start_button.setEnabled(bool(ready_ticked))
        self.start_button.setText(f"▶  Start Batch ({len(ready_ticked)})" if ready_ticked else "▶  Start Batch")
        self.pause_button.setEnabled(JobState.QUEUED in states)
        self.resume_button.setEnabled(JobState.HELD in states)
        self.cancel_button.setEnabled(any(self._active(i) for i in (self.queue_items() or selected)))
        self.cancel_all_button.setEnabled(bool(states & {JobState.QUEUED, JobState.HELD, JobState.RUNNING}))
        self.log_button.setEnabled(bool(self.queue_items() or self.highlighted_items()))
        self.edit_item_button.setEnabled(bool(self.highlighted_items()))
        self.duplicate_preset_button.setEnabled(bool(self.selected_preset()))
        self.retry_button.setEnabled(bool(states & {JobState.FAILED, JobState.CANCELLED}))
        self.clear_completed_button.setEnabled(JobState.COMPLETED in states)
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
        """Queue one export job per ticked, ready video (they run one after another).

        A second click while starting does nothing, and a video that already has a job is never
        queued again, so double clicks cannot start duplicate exports."""
        if self._starting:
            return []
        self._starting = True
        self.start_button.setEnabled(False)
        try:
            return self._start()
        finally:
            self._starting = False
            self._update_buttons()

    def _start(self) -> list[Job]:
        out_dir = self.batch.output_dir
        if not out_dir:
            QMessageBox.warning(self, "Start Batch", "Choose an output folder first.")
            return []
        try:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "Start Batch", f"The output folder cannot be created: {exc}")
            return []
        ready = [i for i in self.selected_items() if self.status(i)[0] == "ready"]
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
        self.summary.setText(f"{len(jobs)} video(s) added to the Export Queue; they are exported one after another."
                             if jobs else self.summary.text())
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

    def cancel_items(self, items: list[BatchItem]) -> None:
        for item in items:
            job = self.job(item) if item is not None else None
            if job is not None and job.state in ACTIVE:
                try:
                    self.services.jobs.cancel(job.id)
                except StudioError as exc:
                    log.warning("Cancel failed", extra={"job_id": job.id, "error": str(exc)})
        self.refresh()

    def cancel_selected(self) -> None:
        self.cancel_items(self.queue_items() or self.selected_items())

    def retry_items(self, items: list[BatchItem]) -> None:
        for item in items:
            job = self.job(item) if item is not None else None
            if job is None or job.state not in (JobState.FAILED, JobState.CANCELLED):
                continue
            output = job.payload.get("output")
            if output and path_key(output) in self.controller.active_outputs():
                continue  # another job writes this file now
            self.services.jobs.retry(job.id)
        self.refresh()

    def retry_failed(self) -> None:
        self.retry_items(self.batch.items)

    def _on_job_changed(self, job: Job) -> None:
        if job.kind == RENDER_FRAME_JOB and job.id in self._compare_jobs and job.state.is_finished:
            side, key = self._compare_jobs.pop(job.id)
            if job.state is JobState.COMPLETED and job.result and getattr(self, "_compare", None):
                self._frame_cache[key] = job.result["image"]
                self._compare[side] = job.result["image"]
                self._show_compare()
            elif job.state is not JobState.COMPLETED:
                self._compare = None
                self.summary.setText(f"⚠ Before / After failed: {job.error or 'cancelled'}")
            return
        if job.kind == RENDER_FRAME_JOB and job.id == self._preview_job and job.state.is_finished:
            self._preview_job = None
            if job.state is JobState.COMPLETED and job.result:
                self.summary.setText("")
                note = " ".join(["Framing and caption style as they will be exported."] + self._preview_notes)
                FramePreviewDialog(job.result["image"], job.result["at"], self, title="Batch Preview",
                                   note=note).exec()
            else:
                self.summary.setText(f"⚠ Preview failed: {job.error or 'cancelled'}")
            self._update_buttons()
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
            self._filling = True
            try:
                for row in range(self.table.rowCount()):
                    if self.table.item(row, COL_VIDEO).data(Qt.ItemDataRole.UserRole) == item.id:
                        self._fill_row(row, item)
                self._fill_queue()
            finally:
                self._filling = False
            self._update_summary()
            self._update_buttons()


def default_output_dir() -> str:
    """A safe default output folder: "AI Video Studio" in the user's Videos folder."""
    movies = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MoviesLocation)
    base = Path(movies) if movies else Path.home() / "Videos"
    return str(base / "AI Video Studio")

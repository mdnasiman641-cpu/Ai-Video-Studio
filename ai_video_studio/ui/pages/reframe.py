"""AI Auto Reframe page: analyse faces/people, choose a subject and format, preview, export.

Analysis and export run as background jobs. The crop path is recomputed from the cached
tracking results whenever a setting changes (no video decoding), and both previews read
the frame the player has already decoded.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio.core.errors import StudioError
from ai_video_studio.core.media import MediaItem, MediaStatus, format_duration
from ai_video_studio.jobs.models import Job, JobState
from ai_video_studio.jobs.reframe_jobs import REFRAME_ANALYZE_JOB, REFRAME_EXPORT_JOB
from ai_video_studio.reframe.detection import opencv_cuda_devices, opencv_version
from ai_video_studio.reframe.framing import (
    PRESET_LABELS,
    CropPath,
    anchor_region,
    output_size_options,
    suitable_subjects,
)
from ai_video_studio.reframe.model import ReframeError, ReframeSettings, TrackingAnalysis
from ai_video_studio.ui.caption_dialogs import FramePreviewDialog
from ai_video_studio.ui.project_controller import JobBridge, ProjectController
from ai_video_studio.ui.widgets.caption_overlay import paint_captions
from ai_video_studio.ui.widgets.common import muted, page_title
from ai_video_studio.ui.widgets.video_preview import VideoPreview

log = logging.getLogger(__name__)

FOLLOWED = QColor("#5fd38d")
OTHER = QColor("#6aa6ff")
CROP = QColor("#ffffff")
SAFE = QColor(255, 184, 77, 70)
STATE_TEXT = {"tracked": "Following subject #{id}", "held": "Subject briefly lost: holding framing",
              "lost": "No subject: returning to centre", "center": "Center crop (no tracking)"}


def _spin(low: float, high: float, step: float, decimals: int = 2, suffix: str = "") -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(low, high)
    box.setSingleStep(step)
    box.setDecimals(decimals)
    if suffix:
        box.setSuffix(suffix)
    box.setKeyboardTracking(False)
    return box


class OutputPreview(QWidget):
    """Paints the reframed output for the player's current frame: crop, scaling and captions."""

    def __init__(self, page: "ReframePage"):
        super().__init__()
        self.page = page
        self.setMinimumSize(180, 240)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

    def output_rect(self) -> QRectF | None:
        size = self.page.output_size()
        if size is None:
            return None
        ow, oh = size
        scale = min(self.width() / ow, self.height() / oh)
        w, h = ow * scale, oh * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            self._paint(painter)
        except Exception:  # noqa: BLE001 - a broken preview must not break the window
            log.exception("Reframe output preview failed")
        finally:
            painter.end()

    def _paint(self, painter: QPainter) -> None:
        painter.fillRect(self.rect(), QColor("#101216"))
        rect = self.output_rect()
        image = self.page.preview.canvas.image()
        crop = self.page.crop_rect_at(self.page.preview.position_seconds())
        if rect is None or image is None or crop is None:
            painter.setPen(QColor("#8b919c"))
            painter.drawText(self.rect().adjusted(8, 8, -8, -8),
                             Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.page.output_placeholder())
            return
        item = self.page.current()
        meta = item.metadata
        kx, ky = image.width() / meta.display_width, image.height() / meta.display_height
        x, y, w, h = crop
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(rect, image, QRectF(x * kx, y * ky, w * kx, h * ky))
        settings = self.page.settings()
        if settings.burn_captions and item.captions is not None:
            texts = [s.text for s in item.captions.active_at(self.page.preview.position_seconds())]
            paint_captions(painter, rect, texts, item.effective_caption_style)
        if settings.caption_safe != "off":
            band = rect.height() * settings.caption_safe_height
            top = rect.bottom() - band if settings.caption_safe == "bottom" else rect.top()
            painter.setPen(QPen(QColor("#ffb84d"), 1, Qt.PenStyle.DashLine))
            painter.drawRect(QRectF(rect.left(), top, rect.width(), band))


class ReframePage(QWidget):
    def __init__(self, controller: ProjectController, bridge: JobBridge):
        super().__init__()
        self.controller = controller
        self.services = controller.services
        self._media_id: str | None = None
        self._loaded_id: str | None = None
        self._refreshing = False
        self._path: CropPath | None = None
        self._path_error = ""
        self._preview_jobs: set[str] = set()
        self._recompute_timer = QTimer(self)
        self._recompute_timer.setSingleShot(True)
        self._recompute_timer.setInterval(120)
        self._recompute_timer.timeout.connect(self.recompute)

        # -- top bar --
        self.media_combo = QComboBox()
        self.media_combo.setMinimumWidth(260)
        self.analyze_button = QPushButton("Analyse Video")
        self.analyze_button.setObjectName("Primary")
        self.analyze_button.setToolTip("Detect and track faces/people (runs in the background)")
        self.cancel_button = QPushButton("Cancel Analysis")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setMaximumWidth(220)
        self.progress.setVisible(False)
        top = QHBoxLayout()
        top.addWidget(QLabel("Video:"))
        top.addWidget(self.media_combo, 1)
        top.addWidget(self.analyze_button)
        top.addWidget(self.cancel_button)
        top.addWidget(self.progress)
        top.addStretch(1)
        self.status = muted("")
        self.warnings = QLabel("")
        self.warnings.setObjectName("Warning")
        self.warnings.setWordWrap(True)
        self.hardware = muted(self._hardware_text())

        # -- previews --
        self.preview = VideoPreview()
        self.preview.set_volume(self.services.settings.get("preview_volume"))
        self.preview.set_overlay(self._paint_source_overlay)
        self.output = OutputPreview(self)
        self.output_info = muted("")
        output_box = QWidget()
        ob = QVBoxLayout(output_box)
        ob.setContentsMargins(0, 0, 0, 0)
        ob.addWidget(QLabel("Reframed output"))
        ob.addWidget(self.output, 1)
        ob.addWidget(self.output_info)
        self.preview_button = QPushButton("Exact Frame Preview")
        self.preview_button.setToolTip("Render the current frame with FFmpeg exactly as it will be exported")
        self.export_button = QPushButton("Export Reframed Video…")
        self.export_button.setObjectName("Primary")
        ob.addWidget(self.preview_button)
        ob.addWidget(self.export_button)
        previews = QSplitter()
        previews.addWidget(self.preview)
        previews.addWidget(output_box)
        previews.setStretchFactor(0, 3)
        previews.setStretchFactor(1, 2)

        # -- 1. analysis --
        self.analysis_fps = _spin(0.5, 15, 1, 1, " frames/s")
        self.analysis_fps.setToolTip("How many frames per second of video are analysed. Higher follows fast "
                                     "movement better but takes longer.")
        self.detect_persons = QCheckBox("Detect full bodies when no face is visible (slower)")
        self.analysis_info = muted("")
        g1 = QGroupBox("1. Analysis")
        f1 = QFormLayout(g1)
        f1.addRow("Analyse:", self.analysis_fps)
        f1.addRow(self.detect_persons)
        f1.addRow(self.analysis_info)

        # -- 2. subject --
        self.mode = QComboBox()
        self.mode.addItem("Follow a subject (tracking)", "track")
        self.mode.addItem("Center crop (no tracking)", "center")
        self.subjects = QListWidget()
        self.subjects.setMinimumHeight(110)
        self.subjects.setToolTip("Select a subject, then choose when to follow it. Double-click to jump to "
                                 "where it first appears.")
        self.follow_button = QPushButton("Follow From Start")
        self.follow_button.setToolTip("Follow the selected subject from the beginning of the video")
        self.switch_button = QPushButton("Follow From Playhead")
        self.switch_button.setToolTip("From the current position onwards, follow the selected subject instead")
        self.clear_switches_button = QPushButton("Clear Subject Changes")
        self.switches_info = muted("")
        g2 = QGroupBox("2. Subject")
        v2 = QVBoxLayout(g2)
        v2.addWidget(self.mode)
        v2.addWidget(self.subjects)
        row = QHBoxLayout()
        row.addWidget(self.follow_button)
        row.addWidget(self.switch_button)
        v2.addLayout(row)
        v2.addWidget(self.clear_switches_button)
        v2.addWidget(self.switches_info)

        # -- 3. format --
        self.preset = QComboBox()
        for key, label in PRESET_LABELS.items():
            self.preset.addItem(label, key)
        self.size_combo = QComboBox()
        g3 = QGroupBox("3. Format")
        f3 = QFormLayout(g3)
        f3.addRow("Aspect ratio:", self.preset)
        f3.addRow("Output size:", self.size_combo)

        # -- 4. framing --
        self.zoom = _spin(1.0, 4.0, 0.1, 2, "×")
        self.zoom.setToolTip("1× is the largest crop that fits the frame; higher values zoom in")
        self.padding = _spin(0, 100, 5, 0, " %")
        self.padding.setToolTip("Space kept around the face (or upper body), as a percentage of its size")
        self.subject_y = _spin(10, 90, 5, 0, " %")
        self.subject_y.setToolTip("Where the subject sits vertically in the frame (0 % = top, 100 % = bottom)")
        self.offset_x = _spin(-50, 50, 1, 0, " %")
        self.offset_x.setToolTip("Moves the frame right (+) or left (−), as a percentage of its width")
        self.offset_y = _spin(-50, 50, 1, 0, " %")
        self.offset_y.setToolTip("Moves the frame down (+) or up (−), as a percentage of its height")
        self.reset_button = QPushButton("Reset to Automatic Framing")
        g4 = QGroupBox("4. Framing")
        f4 = QFormLayout(g4)
        f4.addRow("Zoom:", self.zoom)
        f4.addRow("Padding:", self.padding)
        f4.addRow("Subject height:", self.subject_y)
        f4.addRow("Horizontal offset:", self.offset_x)
        f4.addRow("Vertical offset:", self.offset_y)
        f4.addRow(self.reset_button)

        # -- 5. camera motion --
        self.smoothing = _spin(0, 100, 5, 0, " %")
        self.smoothing.setToolTip("Removes jitter from the detections. Applied without delay.")
        self.follow_speed = _spin(0.05, 5.0, 0.1, 2, " widths/s")
        self.follow_speed.setToolTip("Fastest camera movement, in crop widths per second")
        self.dead_zone = _spin(0, 40, 1, 0, " %")
        self.dead_zone.setToolTip("Movements smaller than this share of the frame do not move the camera")
        self.hold = _spin(0, 10, 0.5, 1, " s")
        self.hold.setToolTip("How long the framing is held when the subject disappears before it returns to centre")
        self.transition = _spin(0, 5, 0.1, 1, " s")
        self.transition.setToolTip("Length of the pan when the followed subject changes or reappears")
        g5 = QGroupBox("5. Camera motion")
        f5 = QFormLayout(g5)
        f5.addRow("Smoothing:", self.smoothing)
        f5.addRow("Max speed:", self.follow_speed)
        f5.addRow("Dead zone:", self.dead_zone)
        f5.addRow("Hold when lost:", self.hold)
        f5.addRow("Transition:", self.transition)

        # -- 6. captions --
        self.caption_safe = QComboBox()
        for label, key in (("Off", "off"), ("Bottom", "bottom"), ("Top", "top")):
            self.caption_safe.addItem(label, key)
        self.caption_safe_height = _spin(5, 50, 1, 0, " %")
        self.burn_captions = QCheckBox("Burn in captions")
        g6 = QGroupBox("6. Captions")
        f6 = QFormLayout(g6)
        f6.addRow("Keep face clear of:", self.caption_safe)
        f6.addRow("Caption area height:", self.caption_safe_height)
        f6.addRow(self.burn_captions)
        f6.addRow(muted("Caption text, timing and style are not changed here; edit them on the Captions page. "
                        "Caption sizes scale with the output height, as for any export."))

        controls = QWidget()
        cl = QVBoxLayout(controls)
        for group in (g1, g2, g3, g4, g5, g6):
            cl.addWidget(group)
        cl.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(controls)
        scroll.setMinimumWidth(360)

        splitter = QSplitter()
        splitter.addWidget(previews)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(page_title("AI Auto Reframe"))
        layout.addLayout(top)
        layout.addWidget(self.status)
        layout.addWidget(self.warnings)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.hardware)

        # -- connections --
        self.media_combo.currentIndexChanged.connect(lambda i: self._select_media(self.media_combo.itemData(i)))
        self.analyze_button.clicked.connect(self.analyze)
        self.cancel_button.clicked.connect(self.cancel_analysis)
        self.preview_button.clicked.connect(self.exact_preview)
        self.export_button.clicked.connect(self.export_video)
        self.follow_button.clicked.connect(self.follow_selected_from_start)
        self.switch_button.clicked.connect(self.follow_selected_from_playhead)
        self.clear_switches_button.clicked.connect(self.clear_switches)
        self.reset_button.clicked.connect(self.reset_framing)
        self.subjects.currentRowChanged.connect(lambda _r: self._update_buttons())
        self.subjects.itemDoubleClicked.connect(self._jump_to_subject)
        for combo in (self.mode, self.preset, self.size_combo, self.caption_safe):
            combo.currentIndexChanged.connect(lambda _i: self._on_control_changed())
        for spin in (self.analysis_fps, self.zoom, self.padding, self.subject_y, self.offset_x, self.offset_y,
                     self.smoothing, self.follow_speed, self.dead_zone, self.hold, self.transition,
                     self.caption_safe_height):
            spin.valueChanged.connect(lambda _v: self._on_control_changed())
        for box in (self.detect_persons, self.burn_captions):
            box.toggled.connect(lambda _v: self._on_control_changed())
        self.preview.position_changed.connect(lambda _ms: self.output.update())
        self.preview.canvas.sink.videoFrameChanged.connect(lambda _f: self.output.update())

        controller.media_list_changed.connect(self.refresh_media)
        controller.media_updated.connect(self._on_media_updated)
        controller.captions_changed.connect(self._on_captions_changed)
        controller.reframe_changed.connect(self._on_reframe_changed)
        controller.reframe_analysis_finished.connect(self._on_analysis_finished)
        bridge.job_changed.connect(self._on_job_changed)
        self.refresh_media()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _hardware_text() -> str:
        version = opencv_version()
        if version is None:
            return ("OpenCV is not installed: analysis is unavailable (Center crop still works). "
                    "Run: pip install -r requirements.txt")
        cuda = opencv_cuda_devices()
        detection = (f"Detection: OpenCV {version} on the CPU" +
                     ("" if cuda else " (this OpenCV build has no CUDA support; that is normal for pip builds)"))
        return (f"{detection}. Export encoder: chosen at export from Settings; NVIDIA NVENC is used only if a "
                f"test encode succeeds, otherwise the CPU encoder (libx264).")

    def current(self) -> MediaItem | None:
        if self.controller.project is None or self._media_id is None:
            return None
        return self.controller.project.find_media(self._media_id)

    def analysis(self) -> TrackingAnalysis | None:
        item = self.current()
        return item.reframe.analysis if item is not None and item.reframe is not None else None

    def settings(self) -> ReframeSettings:
        if self._media_id is None or self.current() is None:
            return ReframeSettings()
        return self.controller.reframe_state(self._media_id).settings

    def output_size(self) -> tuple[int, int] | None:
        item = self.current()
        if item is None or item.metadata is None:
            return None
        return self.controller.reframe_output_size(self._media_id)

    def crop_rect_at(self, t: float) -> tuple[int, int, int, int] | None:
        if self._path is None:
            return None
        x, y = self._path.at(t)
        return x, y, self._path.crop_w, self._path.crop_h

    def output_placeholder(self) -> str:
        item = self.current()
        if item is None:
            return "No video selected."
        if item.metadata is None:
            return "Reading video information…"
        if self._path_error:
            return self._path_error
        return "Loading preview…"

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
        self._sync_preview()
        self._load_controls()
        self.recompute()
        self._show_active_analysis()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._sync_preview()

    def _sync_preview(self) -> None:
        """Load the clip into the player only while visible."""
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
        """Called when the window closes: no pending recompute may run after services are closed."""
        self._recompute_timer.stop()
        # pause(), not stop(): stop() emits LoadedMedia synchronously, and VideoPreview reacts to
        # that by calling pause() inside stop(), which can deadlock Qt's FFmpeg media backend.
        self.preview.player.pause()

    def _on_media_updated(self, media_id: str) -> None:
        if media_id == self._media_id:
            self._load_controls()
            self.recompute()

    def _on_captions_changed(self, media_id: str) -> None:
        if media_id == self._media_id:
            self._load_controls()
            self.output.update()

    def _on_reframe_changed(self, media_id: str) -> None:
        if media_id == self._media_id:
            self._recompute_timer.start()

    # ------------------------------------------------------------------ controls <-> settings
    def _load_controls(self) -> None:
        self._refreshing = True
        try:
            item = self.current()
            s = self.settings()
            self.analysis_fps.setValue(s.analysis_fps)
            self.detect_persons.setChecked(s.detect_persons)
            self.mode.setCurrentIndex(max(0, self.mode.findData(s.mode)))
            self.preset.setCurrentIndex(max(0, self.preset.findData(s.preset)))
            self._fill_output_sizes(s)
            self.zoom.setValue(s.zoom)
            self.padding.setValue(s.padding * 100)
            self.subject_y.setValue(s.subject_y * 100)
            self.offset_x.setValue(s.offset_x * 100)
            self.offset_y.setValue(s.offset_y * 100)
            self.smoothing.setValue(s.smoothing * 100)
            self.follow_speed.setValue(s.follow_speed)
            self.dead_zone.setValue(s.dead_zone * 100)
            self.hold.setValue(s.hold_seconds)
            self.transition.setValue(s.transition_seconds)
            self.caption_safe.setCurrentIndex(max(0, self.caption_safe.findData(s.caption_safe)))
            self.caption_safe_height.setValue(s.caption_safe_height * 100)
            count = len(item.captions.segments) if item and item.captions else 0
            self.burn_captions.setChecked(s.burn_captions)
            self.burn_captions.setText(f"Burn in captions ({count})" if count else "Burn in captions (none yet)")
            self._fill_subjects(s)
        finally:
            self._refreshing = False
        self._update_analysis_info()
        self._update_buttons()

    def _fill_output_sizes(self, s: ReframeSettings) -> None:
        self.size_combo.clear()
        item = self.current()
        if item is None or item.metadata is None:
            return
        meta = item.metadata
        for w, h in output_size_options(s.preset, meta.display_width, meta.display_height):
            self.size_combo.addItem(f"{w} × {h}", f"{w}x{h}")
        index = self.size_combo.findData(f"{s.output_width}x{s.output_height}") if s.output_width else 0
        self.size_combo.setCurrentIndex(max(0, index))

    def _fill_subjects(self, s: ReframeSettings) -> None:
        self.subjects.clear()
        auto = QListWidgetItem(("● " if s.target is None else "") + "Automatic (best visible subject)")
        auto.setData(Qt.ItemDataRole.UserRole, None)
        self.subjects.addItem(auto)
        analysis = self.analysis()
        selected_row = 0
        if analysis is not None:
            for track in suitable_subjects(analysis):
                marker = "● " if s.target == track.id else ""
                text = (f"{marker}#{track.id} {track.kind.capitalize()} · {format_duration(track.start)}–"
                        f"{format_duration(track.end)} · seen {analysis.coverage(track):.0%} · "
                        f"confidence {track.mean_confidence:.2f}")
                entry = QListWidgetItem(text)
                entry.setData(Qt.ItemDataRole.UserRole, track.id)
                self.subjects.addItem(entry)
                if s.target == track.id:
                    selected_row = self.subjects.count() - 1
        self.subjects.setCurrentRow(selected_row)
        if s.switches:
            changes = ", ".join(f"{format_duration(float(t))} → " + ("auto" if tid is None else f"#{tid}")
                                for t, tid in sorted(s.switches, key=lambda r: float(r[0])))
            self.switches_info.setText(f"Subject changes: {changes}")
        else:
            self.switches_info.setText("No subject changes.")

    def _selected_track(self) -> int | None:
        entry = self.subjects.currentItem()
        return entry.data(Qt.ItemDataRole.UserRole) if entry is not None else None

    def _settings_from_controls(self) -> ReframeSettings:
        current = self.settings()
        data = self.size_combo.currentData() if self.size_combo.currentIndex() > 0 else None
        size = [int(v) for v in data.split("x")] if data else None
        return ReframeSettings(
            mode=self.mode.currentData(), preset=self.preset.currentData(),
            output_width=size[0] if size else 0, output_height=size[1] if size else 0,
            target=current.target, switches=[list(r) for r in current.switches],
            zoom=self.zoom.value(), padding=self.padding.value() / 100, subject_y=self.subject_y.value() / 100,
            offset_x=self.offset_x.value() / 100, offset_y=self.offset_y.value() / 100,
            smoothing=self.smoothing.value() / 100, follow_speed=self.follow_speed.value(),
            dead_zone=self.dead_zone.value() / 100, hold_seconds=self.hold.value(),
            transition_seconds=self.transition.value(), caption_safe=self.caption_safe.currentData(),
            caption_safe_height=self.caption_safe_height.value() / 100, burn_captions=self.burn_captions.isChecked(),
            analysis_fps=self.analysis_fps.value(), detect_persons=self.detect_persons.isChecked(),
        )

    def _on_control_changed(self) -> None:
        if self._refreshing or self.current() is None:
            return
        previous = self.settings()
        settings = self._settings_from_controls()
        if settings.preset != previous.preset:
            settings.output_width = settings.output_height = 0  # sizes differ per preset
            self._refreshing = True
            self._fill_output_sizes(settings)
            self._refreshing = False
        self._apply(settings)

    def _apply(self, settings: ReframeSettings) -> None:
        try:
            self.controller.set_reframe_settings(self._media_id, settings)
        except StudioError as exc:
            self.status.setText(f"⚠ {exc}")
            return
        self._update_buttons()

    def follow_selected_from_start(self) -> None:
        if self.current() is None:
            return
        settings = self._settings_from_controls()
        settings.target = self._selected_track()
        self._apply(settings)
        self._load_controls()

    def follow_selected_from_playhead(self) -> None:
        if self.current() is None:
            return
        t = round(self.preview.position_seconds(), 3)
        settings = self._settings_from_controls()
        settings.switches = [r for r in settings.switches if abs(float(r[0]) - t) > 1e-3] + [[t, self._selected_track()]]
        self._apply(settings)
        self._load_controls()

    def clear_switches(self) -> None:
        if self.current() is None:
            return
        settings = self._settings_from_controls()
        settings.switches = []
        self._apply(settings)
        self._load_controls()

    def reset_framing(self) -> None:
        """Back to automatic subject choice and the default framing and motion settings."""
        if self.current() is None:
            return
        current = self._settings_from_controls()
        defaults = ReframeSettings()
        keep = {k: getattr(current, k) for k in ("preset", "output_width", "output_height", "caption_safe",
                                                 "caption_safe_height", "burn_captions", "analysis_fps",
                                                 "detect_persons")}
        settings = ReframeSettings(**(defaults.to_dict() | keep))
        self._apply(settings)
        self._load_controls()

    def _jump_to_subject(self, entry: QListWidgetItem) -> None:
        analysis = self.analysis()
        track_id = entry.data(Qt.ItemDataRole.UserRole)
        track = analysis.track(track_id) if analysis is not None and track_id is not None else None
        if track is not None:
            self.preview.seek(int(track.start * 1000))

    # ------------------------------------------------------------------ crop path
    def recompute(self) -> None:
        self._path, self._path_error = None, ""
        item = self.current()
        if item is not None and item.metadata is not None:
            try:
                self._path = self.controller.reframe_crop_path(self._media_id)
            except StudioError as exc:
                self._path_error = str(exc)
        self._update_warnings()
        self._update_output_info()
        self._update_buttons()
        self.preview.refresh_overlay()
        self.output.update()

    def _update_warnings(self) -> None:
        item = self.current()
        messages = []
        analysis = self.analysis()
        if item is not None and self.controller.analysis_is_stale(item):
            messages.append("The video file changed after it was analysed. Analyse it again.")
        if analysis is not None and self.settings().mode == "track":
            messages += analysis.warnings
        if self._path is not None:
            messages += self._path.warnings
        elif self._path_error:
            messages.append(self._path_error)
        self.warnings.setText("\n".join(f"⚠ {m}" for m in messages))
        self.warnings.setVisible(bool(messages))

    def _update_output_info(self) -> None:
        if self._path is None or self.output_size() is None:
            self.output_info.setText("")
            return
        ow, oh = self.output_size()
        scale = ow / self._path.crop_w
        detail = (f"upscaled {scale:.2f}×" if scale > 1.01 else f"downscaled {scale:.2f}×" if scale < 0.99
                  else "not scaled")
        tracked = (f" · subject in frame {self._path.tracked_fraction:.0%} of the time"
                   if self.settings().mode == "track" else "")
        self.output_info.setText(f"Output {ow}×{oh} · crop {self._path.crop_w}×{self._path.crop_h} ({detail}){tracked}")

    def _update_analysis_info(self) -> None:
        analysis = self.analysis()
        if analysis is None:
            self.analysis_info.setText("Not analysed yet.")
            return
        faces = sum(1 for t in analysis.tracks if t.kind == "face")
        persons = len(analysis.tracks) - faces
        self.analysis_info.setText(
            f"{analysis.frames_analyzed} frames analysed at {analysis.sample_fps:g}/s · {faces} face(s), "
            f"{persons} full-body subject(s) · {analysis.detector}")

    def _update_buttons(self) -> None:
        item = self.current()
        usable = item is not None and item.status is not MediaStatus.MISSING and item.metadata is not None
        running = bool(item and self.controller.active_jobs(self._media_id, REFRAME_ANALYZE_JOB))
        self.analyze_button.setEnabled(usable and not running and opencv_version() is not None)
        self.cancel_button.setEnabled(running)
        tracking = self.mode.currentData() == "track"
        has_analysis = self.analysis() is not None
        self.subjects.setEnabled(tracking and has_analysis)
        self.follow_button.setEnabled(tracking and has_analysis and self.subjects.currentRow() >= 0)
        self.switch_button.setEnabled(tracking and has_analysis and self.subjects.currentRow() >= 0)
        self.clear_switches_button.setEnabled(tracking and bool(self.settings().switches))
        has_caps = bool(item and item.captions and item.captions.segments)
        self.burn_captions.setEnabled(has_caps)
        self.export_button.setEnabled(usable and self._path is not None)
        self.preview_button.setEnabled(usable and self._path is not None)

    # ------------------------------------------------------------------ overlay
    def _paint_source_overlay(self, painter: QPainter, rect: QRectF, size: QSize) -> None:
        item = self.current()
        if item is None or item.metadata is None:
            return
        meta = item.metadata
        k = rect.width() / meta.display_width
        t = self.preview.position_seconds()
        painter.save()
        crop = self.crop_rect_at(t)
        settings = self.settings()
        if crop is not None:
            x, y, w, h = crop
            box = QRectF(rect.left() + x * k, rect.top() + y * k, w * k, h * k)
            shade = QColor(0, 0, 0, 140)
            painter.fillRect(QRectF(rect.left(), rect.top(), rect.width(), box.top() - rect.top()), shade)
            painter.fillRect(QRectF(rect.left(), box.bottom(), rect.width(), rect.bottom() - box.bottom()), shade)
            painter.fillRect(QRectF(rect.left(), box.top(), box.left() - rect.left(), box.height()), shade)
            painter.fillRect(QRectF(box.right(), box.top(), rect.right() - box.right(), box.height()), shade)
            if settings.caption_safe != "off":
                band = box.height() * settings.caption_safe_height
                top = box.bottom() - band if settings.caption_safe == "bottom" else box.top()
                painter.fillRect(QRectF(box.left(), top, box.width(), band), SAFE)
            painter.setPen(QPen(CROP, 2))
            painter.drawRect(box)
        analysis = self.analysis()
        followed = self._path.followed_at(t) if self._path is not None else None
        font = QFont(painter.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() * 0.9))
        painter.setFont(font)
        if analysis is not None and settings.mode == "track":
            tolerance = analysis.sample_interval * 0.75
            for track in analysis.tracks:
                sample = track.box_at(t, tolerance, settings.hold_seconds)
                if sample is None:
                    continue
                color = FOLLOWED if track.id == followed else OTHER
                bx, by, bw, bh = sample.x * rect.width(), sample.y * rect.height(), sample.w * rect.width(), sample.h * rect.height()
                painter.setPen(QPen(color, 2 if track.id == followed else 1))
                painter.drawRect(QRectF(rect.left() + bx, rect.top() + by, bw, bh))
                if track.kind == "person":
                    ax, ay, aw, ah = anchor_region("person", bx, by, bw, bh)
                    painter.setPen(QPen(color, 1, Qt.PenStyle.DotLine))
                    painter.drawRect(QRectF(rect.left() + ax, rect.top() + ay, aw, ah))
                painter.setPen(color)
                painter.drawText(QRectF(rect.left() + bx, rect.top() + by - 16, 260, 16),
                                 f"#{track.id} {track.kind} {sample.confidence:.2f}")
        if self._path is not None:
            state = self._path.state_at(t)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(QRectF(rect.left() + 6, rect.top() + 4, rect.width() - 12, 18),
                             STATE_TEXT[state].format(id=followed))
        painter.restore()

    # ------------------------------------------------------------------ jobs
    def analyze(self) -> Job | None:
        if self.current() is None:
            return None
        self._on_control_changed()  # make sure the analysis options shown are stored
        try:
            job = self.controller.submit_reframe_analysis(self._media_id)
        except StudioError as exc:
            QMessageBox.warning(self, "Analyse Video", str(exc))
            return None
        self.status.setText("Analysis queued.")
        self._update_buttons()
        return job

    def cancel_analysis(self) -> None:
        for job in self.controller.active_jobs(self._media_id, REFRAME_ANALYZE_JOB) if self._media_id else []:
            try:
                self.services.jobs.cancel(job.id)
            except StudioError as exc:
                self.status.setText(f"⚠ {exc}")

    def _show_active_analysis(self) -> None:
        jobs = self.controller.active_jobs(self._media_id, REFRAME_ANALYZE_JOB) if self._media_id else []
        if jobs:
            self._show_job(jobs[0])
        else:
            self.progress.setVisible(False)
            self.status.setText("")

    def _on_analysis_finished(self, media_id: str, job: Job) -> None:
        if media_id != self._media_id:
            return
        self._load_controls()
        self.recompute()
        self._show_job(job)

    def _on_job_changed(self, job: Job) -> None:
        if job.id in self._preview_jobs and job.state.is_finished:
            self._preview_jobs.discard(job.id)
            if job.state is JobState.COMPLETED:
                self.status.setText("")
                FramePreviewDialog(job.result["image"], job.result["at"], self, title="Exact Reframe Render",
                                   note="Rendered by FFmpeg exactly as the export will crop, scale and caption "
                                        "this frame.").exec()
                try:
                    Path(job.result["image"]).unlink(missing_ok=True)
                except OSError:
                    pass
            elif job.state is JobState.FAILED:
                self.status.setText(f"⚠ Preview failed: {job.error}")
            return
        if job.payload.get("media_id") != self._media_id or job.kind not in (REFRAME_ANALYZE_JOB, REFRAME_EXPORT_JOB):
            return
        self._show_job(job)
        self._update_buttons()

    def _show_job(self, job: Job) -> None:
        label = "Analysis" if job.kind == REFRAME_ANALYZE_JOB else "Reframed export"
        running = job.state in (JobState.RUNNING, JobState.QUEUED)
        self.progress.setVisible(running)
        self.progress.setValue(int(job.progress * 100))
        if job.state is JobState.RUNNING:
            self.status.setText(f"{label}… {int(job.progress * 100)}%")
        elif job.state is JobState.QUEUED:
            self.status.setText(f"{label}: queued")
        elif job.state is JobState.FAILED:
            self.status.setText(f"⚠ {label} failed: {job.error}")
        elif job.state is JobState.CANCELLED:
            self.status.setText(f"{label} cancelled.")
        elif job.state is JobState.COMPLETED and job.result:
            if job.kind == REFRAME_ANALYZE_JOB:
                notes = " ".join(job.result.get("notes", []))
                self.status.setText(f"Analysis finished: {job.result.get('tracks', 0)} subject(s) found. {notes}")
            else:
                warnings = " ⚠ " + " ".join(job.result.get("warnings", [])) if job.result.get("warnings") else ""
                self.status.setText(f"Exported {job.result['output']} ({job.result['encoder']}).{warnings}")

    def exact_preview(self) -> Job | None:
        if self.current() is None:
            return None
        try:
            job = self.controller.submit_reframe_preview(self._media_id, self.preview.position_seconds())
        except StudioError as exc:
            QMessageBox.warning(self, "Exact Frame Preview", str(exc))
            return None
        self._preview_jobs.add(job.id)
        self.status.setText("Rendering frame with FFmpeg…")
        return job

    def export_video(self) -> None:
        item = self.current()
        if item is None or self._path is None:
            return
        problems = list(self._path.warnings)
        analysis = self.analysis()
        if analysis is not None and self.settings().mode == "track":
            problems = analysis.warnings + problems
        if self.controller.analysis_is_stale(item):
            problems.insert(0, "The video file changed after it was analysed.")
        if problems:
            answer = QMessageBox.question(
                self, "Export Reframed Video",
                "Check these points before exporting:\n\n" + "\n".join(f"• {p}" for p in problems)
                + "\n\nExport anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        source = Path(item.path)
        preset = self.settings().preset.replace(":", "x")
        suggested = str(source.with_name(f"{source.stem}_reframed_{preset}.mp4"))
        path, _ = QFileDialog.getSaveFileName(self, "Export Reframed Video", suggested, "MP4 video (*.mp4)")
        if not path:
            return
        if Path(path).suffix.lower() != ".mp4":
            path += ".mp4"
        self.start_export(path)

    def start_export(self, output: str) -> Job | None:
        try:
            job = self.controller.submit_reframe_export(self._media_id, output, self._path)
        except (StudioError, ReframeError) as exc:
            QMessageBox.warning(self, "Export Reframed Video", str(exc))
            return None
        self.status.setText(f"Export queued: {Path(output).name}")
        return job

"""Resize & Crop page: output size, fit mode, position, background, captions and export.

The output preview draws the current video frame with the same :class:`Layout` the export
uses (source rectangle → canvas rectangle), so what you see is what FFmpeg renders. The blurred
background is approximated on screen; **Exact Frame Preview** renders the frame with FFmpeg.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio.core.errors import StudioError
from ai_video_studio.core.media import MediaItem, MediaStatus, format_duration
from ai_video_studio.jobs.models import Job, JobState
from ai_video_studio.jobs.render_jobs import RENDER_JOB
from ai_video_studio.reframe.framing import CropPath
from ai_video_studio.render.layout import MODES, PRESETS, Layout, ResizeSettings, aspect_text
from ai_video_studio.render.pipeline import kept_duration
from ai_video_studio.ui.caption_dialogs import FramePreviewDialog
from ai_video_studio.ui.project_controller import JobBridge, ProjectController
from ai_video_studio.ui.widgets.caption_overlay import paint_segments
from ai_video_studio.ui.widgets.common import muted, open_folder, page_title
from ai_video_studio.ui.widgets.output_settings import ExportDialog
from ai_video_studio.ui.widgets.video_preview import VideoPreview

log = logging.getLogger(__name__)

CROP_LINE = QColor("#ffb84d")
SAFE = QColor(255, 255, 255, 120)
UI_ZONE = QColor(255, 184, 77, 45)


class CanvasPreview(QWidget):
    """Paints the output canvas: background, the video region, captions and safe areas."""

    def __init__(self, page: "ResizePage"):
        super().__init__()
        self.page = page
        self.setMinimumSize(240, 240)

    def canvas_rect(self, out_w: int, out_h: int) -> QRectF:
        k = min((self.width() - 8) / out_w, (self.height() - 8) / out_h)
        w, h = out_w * k, out_h * k
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#11141a"))
        try:
            self._paint(painter)
        except Exception:  # noqa: BLE001 - a preview problem must never break the page
            log.exception("Canvas preview failed")
        painter.end()

    def _paint(self, painter: QPainter) -> None:
        page = self.page
        layout, image = page.layout, page.preview.canvas.image()
        if layout is None:
            painter.setPen(QColor("#8a93a6"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                             page.layout_error or "Choose a video.")
            return
        rect = self.canvas_rect(layout.out_w, layout.out_h)
        kx, ky = rect.width() / layout.out_w, rect.height() / layout.out_h
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        item = page.current()
        meta = item.metadata if item else None
        src_rect = page.source_rect_at(page.preview.position_seconds())
        if layout.background == "blur" and image is not None:
            # On-screen approximation of the blurred background: a tiny copy scaled up.
            small = image.scaled(QSize(max(4, layout.out_w // 24), max(4, layout.out_h // 24)),
                                 Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                 Qt.TransformationMode.SmoothTransformation)
            painter.drawImage(rect, small, self._cover(small, layout.out_w / layout.out_h))
        else:
            painter.fillRect(rect, QColor(layout.color if layout.background == "color" else "#000000"))
        dst = QRectF(rect.x() + layout.dst_x * kx, rect.y() + layout.dst_y * ky, layout.dst_w * kx, layout.dst_h * ky)
        if page.settings.mode == "reframe":
            dst = rect
        if image is not None and meta is not None and src_rect is not None:
            sx, sy = image.width() / meta.display_width, image.height() / meta.display_height
            x, y, w, h = src_rect
            painter.drawImage(dst, image, QRectF(x * sx, y * sy, w * sx, h * sy))
        elif image is None:
            painter.setPen(QColor("#8a93a6"))
            painter.drawText(dst, Qt.AlignmentFlag.AlignCenter, "Play or seek the video to see a frame")
        if page.settings.burn_captions and item is not None and item.captions:
            t = page.preview.position_seconds()
            paint_segments(painter, rect, item.captions.active_at(t), item.effective_caption_style)
        if page.show_safe.isChecked():
            painter.setPen(QPen(SAFE, 1, Qt.PenStyle.DashLine))
            painter.drawRect(rect.adjusted(rect.width() * 0.05, rect.height() * 0.05,
                                           -rect.width() * 0.05, -rect.height() * 0.05))
            if layout.out_h > layout.out_w:  # vertical apps overlay buttons and text here
                painter.fillRect(QRectF(rect.x(), rect.y(), rect.width(), rect.height() * 0.08), UI_ZONE)
                painter.fillRect(QRectF(rect.x(), rect.bottom() - rect.height() * 0.2, rect.width(),
                                        rect.height() * 0.2), UI_ZONE)
                painter.fillRect(QRectF(rect.right() - rect.width() * 0.15, rect.y() + rect.height() * 0.4,
                                        rect.width() * 0.15, rect.height() * 0.4), UI_ZONE)
        painter.setPen(QPen(QColor("#5a6375"), 1))
        painter.drawRect(rect)

    @staticmethod
    def _cover(image: QImage, aspect: float) -> QRectF:
        w, h = image.width(), image.height()
        if w / h > aspect:
            cw = h * aspect
            return QRectF((w - cw) / 2, 0, cw, h)
        ch = w / aspect
        return QRectF(0, (h - ch) / 2, w, ch)


class ResizePage(QWidget):
    def __init__(self, controller: ProjectController, bridge: JobBridge):
        super().__init__()
        self.controller = controller
        self.services = controller.services
        self._media_id: str | None = None
        self._loaded_id: str | None = None
        self._filling = False
        self.settings = ResizeSettings()
        self.layout: Layout | None = None
        self.layout_error = ""
        self._reframe_path: CropPath | None = None
        self._export_jobs: dict[str, str] = {}
        self._preview_jobs: set[str] = set()

        self.media_combo = QComboBox()
        self.media_combo.setMinimumWidth(320)
        self.preview = VideoPreview()
        self.preview.set_volume(self.services.settings.get("preview_volume"))
        self.preview.set_overlay(self._paint_source_overlay)
        self.canvas = CanvasPreview(self)

        self.preset = QComboBox()
        for key, (label, _size) in PRESETS.items():
            self.preset.addItem(label, key)
        self.width_spin, self.height_spin = QSpinBox(), QSpinBox()
        for box, limit in ((self.width_spin, 7680), (self.height_spin, 4320)):
            box.setRange(64, limit)
            box.setSingleStep(2)
            box.setSuffix(" px")
            box.setKeyboardTracking(False)
        self.mode = QComboBox()
        for key, label in MODES.items():
            self.mode.addItem(label, key)
        self.pos_x, self.pos_y = QSlider(Qt.Orientation.Horizontal), QSlider(Qt.Orientation.Horizontal)
        for slider in (self.pos_x, self.pos_y):
            slider.setRange(0, 100)
        self.zoom = QSpinBox()
        self.zoom.setRange(10, 400)
        self.zoom.setSuffix(" % of Fit")
        self.zoom.setKeyboardTracking(False)
        self.crop_boxes = []
        for _ in range(4):
            box = QDoubleSpinBox()
            box.setRange(0, 100)
            box.setDecimals(1)
            box.setSuffix(" %")
            box.setKeyboardTracking(False)
            self.crop_boxes.append(box)
        self.lock_aspect = QCheckBox("Keep the output aspect ratio (no padding)")
        self.color_button = QPushButton("Background colour…")
        self.blur = QSpinBox()
        self.blur.setRange(1, 60)
        self.blur.setKeyboardTracking(False)
        self.burn = QCheckBox("Burn in captions (scaled to the output canvas)")
        self.use_cuts = QCheckBox("Apply Smart Cut cuts")
        self.show_safe = QCheckBox("Show safe areas")
        self.show_safe.setChecked(True)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.warnings = QLabel("")
        self.warnings.setObjectName("Warning")
        self.warnings.setWordWrap(True)
        self.exact_button = QPushButton("Exact Frame Preview")
        self.export_button = QPushButton("Export…")
        self.export_button.setObjectName("Primary")
        self.reset_button = QPushButton("Reset")
        self.export_status = muted("")
        self.open_folder_button = QPushButton("Open Output Folder")
        self.cancel_export_button = QPushButton("Cancel Export")
        self.retry_export_button = QPushButton("Retry Export")

        size_box = QGroupBox("Output")
        sf = QFormLayout(size_box)
        sf.addRow("Size:", self.preset)
        row = QHBoxLayout()
        row.addWidget(self.width_spin)
        row.addWidget(QLabel("×"))
        row.addWidget(self.height_spin)
        sf.addRow("Custom size:", row)
        sf.addRow("Mode:", self.mode)
        place_box = QGroupBox("Position and crop")
        pf = QFormLayout(place_box)
        pf.addRow("Horizontal:", self.pos_x)
        pf.addRow("Vertical:", self.pos_y)
        pf.addRow("Size:", self.zoom)
        labels = ("Crop left:", "Crop top:", "Crop width:", "Crop height:")
        for label, box in zip(labels, self.crop_boxes):
            pf.addRow(label, box)
        pf.addRow("", self.lock_aspect)
        look_box = QGroupBox("Background and captions")
        lf = QFormLayout(look_box)
        lf.addRow("Colour:", self.color_button)
        lf.addRow("Blur strength:", self.blur)
        lf.addRow("", self.burn)
        lf.addRow("", self.use_cuts)
        lf.addRow("", self.show_safe)
        controls = QWidget()
        cl = QVBoxLayout(controls)
        for w in (size_box, place_box, look_box, self.info, self.warnings):
            cl.addWidget(w)
        buttons = QHBoxLayout()
        for b in (self.exact_button, self.export_button, self.reset_button):
            buttons.addWidget(b)
        cl.addLayout(buttons)
        cl.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(controls)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(340)

        source_box = QWidget()
        sl = QVBoxLayout(source_box)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(muted("Source (orange: the part used)", wrap=False))
        sl.addWidget(self.preview, 1)
        output_box = QWidget()
        ol = QVBoxLayout(output_box)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.addWidget(muted("Output canvas", wrap=False))
        ol.addWidget(self.canvas, 1)
        splitter = QSplitter()
        splitter.addWidget(source_box)
        splitter.addWidget(output_box)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setStretchFactor(2, 1)

        top = QHBoxLayout()
        top.addWidget(QLabel("Video:"))
        top.addWidget(self.media_combo)
        top.addStretch(1)
        export_row = QHBoxLayout()
        export_row.addWidget(self.export_status, 1)
        for b in (self.cancel_export_button, self.retry_export_button, self.open_folder_button):
            export_row.addWidget(b)
        layout = QVBoxLayout(self)
        layout.addWidget(page_title("Resize & Crop"))
        layout.addLayout(top)
        layout.addWidget(splitter, 1)
        layout.addLayout(export_row)

        self.media_combo.currentIndexChanged.connect(
            lambda i: self._select_media(self.media_combo.itemData(i) if i >= 0 else None))
        for box in (self.preset, self.mode):
            box.currentIndexChanged.connect(self._on_control_changed)
        for w in [self.width_spin, self.height_spin, self.zoom, self.blur, self.pos_x, self.pos_y] + self.crop_boxes:
            w.valueChanged.connect(self._on_control_changed)
        for box in (self.lock_aspect, self.burn, self.use_cuts):
            box.toggled.connect(self._on_control_changed)
        self.show_safe.toggled.connect(self.canvas.update)
        self.color_button.clicked.connect(self._choose_color)
        self.exact_button.clicked.connect(self.exact_preview)
        self.export_button.clicked.connect(self.export_video)
        self.reset_button.clicked.connect(self.reset)
        self.open_folder_button.clicked.connect(self.open_output_folder)
        self.cancel_export_button.clicked.connect(self.cancel_export)
        self.retry_export_button.clicked.connect(self.retry_export)
        self.preview.position_changed.connect(lambda _ms: self.canvas.update())
        self.preview.canvas.sink.videoFrameChanged.connect(lambda _f: self.canvas.update())
        controller.media_list_changed.connect(self.refresh_media)
        controller.media_updated.connect(lambda mid: mid == self._media_id and self._load())
        controller.resize_changed.connect(lambda mid: mid == self._media_id and not self._filling and self._load())
        controller.captions_changed.connect(lambda mid: mid == self._media_id and self._recompute())
        controller.smartcut_changed.connect(lambda mid: mid == self._media_id and self._update_cuts())
        controller.reframe_changed.connect(lambda mid: mid == self._media_id and self._recompute())
        bridge.job_changed.connect(self._on_job_changed)
        self.services.settings.subscribe(lambda k, v: k == "preview_volume" and self.preview.set_volume(v))
        self.refresh_media()

    # ------------------------------------------------------------------ media selection
    def current(self) -> MediaItem | None:
        project = self.controller.project
        return project.find_media(self._media_id) if project and self._media_id else None

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
        self._load()
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

    # ------------------------------------------------------------------ controls
    def _load(self) -> None:
        item = self.current()
        self.settings = self.controller.resize_settings(self._media_id) if item else ResizeSettings()
        s = self.settings
        self._filling = True
        try:
            self.preset.setCurrentIndex(max(0, self.preset.findData(s.preset)))
            self.mode.setCurrentIndex(max(0, self.mode.findData(s.mode)))
            self.width_spin.setValue(int(s.width))
            self.height_spin.setValue(int(s.height))
            self.pos_x.setValue(round(s.pos_x * 100))
            self.pos_y.setValue(round(s.pos_y * 100))
            self.zoom.setValue(round(s.zoom * 100))
            for box, value in zip(self.crop_boxes, (s.crop_x, s.crop_y, s.crop_w, s.crop_h)):
                box.setValue(value * 100)
            self.lock_aspect.setChecked(s.lock_aspect)
            self.blur.setValue(int(s.blur))
            self.burn.setChecked(s.burn_captions)
            self.use_cuts.setChecked(s.use_cuts)
        finally:
            self._filling = False
        self._recompute()

    def _settings_from_controls(self) -> ResizeSettings:
        crop = [box.value() / 100 for box in self.crop_boxes]
        return ResizeSettings(
            preset=self.preset.currentData(), width=self.width_spin.value(), height=self.height_spin.value(),
            mode=self.mode.currentData(), pos_x=self.pos_x.value() / 100, pos_y=self.pos_y.value() / 100,
            zoom=self.zoom.value() / 100, crop_x=crop[0], crop_y=crop[1], crop_w=max(0.01, crop[2]),
            crop_h=max(0.01, crop[3]), lock_aspect=self.lock_aspect.isChecked(), background=self.settings.background,
            blur=self.blur.value(), burn_captions=self.burn.isChecked(), use_cuts=self.use_cuts.isChecked())

    def _on_control_changed(self, *_args) -> None:
        if self._filling or self.current() is None:
            return
        s = self._settings_from_controls()
        try:
            s.validate()
        except StudioError as exc:
            self.layout, self.layout_error = None, str(exc)
            self.warnings.setText(f"⚠ {exc}")
            self.canvas.update()
            return
        self.settings = s
        self._filling = True
        try:
            self.controller.set_resize_settings(self._media_id, s)
        finally:
            self._filling = False
        self._recompute()

    def _choose_color(self) -> None:
        color = QColorDialog.getColor(QColor(self.settings.background), self, "Background colour")
        if color.isValid():
            self.settings.background = color.name()
            self._on_control_changed()

    def reset(self) -> None:
        if self.current() is not None:
            self.controller.set_resize_settings(self._media_id, ResizeSettings())
            self._load()

    def _update_enabled(self) -> None:
        mode = self.settings.mode
        custom_size = self.settings.preset == "custom"
        self.width_spin.setEnabled(custom_size)
        self.height_spin.setEnabled(custom_size)
        self.pos_x.setEnabled(mode in ("fill", "custom"))
        self.pos_y.setEnabled(mode in ("fill", "custom"))
        self.zoom.setEnabled(mode == "custom")
        for box in self.crop_boxes:
            box.setEnabled(mode == "crop")
        self.lock_aspect.setEnabled(mode == "crop")
        self.color_button.setEnabled(mode in ("fit", "crop", "custom"))
        self.color_button.setStyleSheet(f"background: {self.settings.background};")
        self.blur.setEnabled(mode == "blur")
        item = self.current()
        self.burn.setEnabled(bool(item and item.captions and item.captions.segments))
        ready = self.layout is not None and item is not None and item.status is not MediaStatus.MISSING
        self.exact_button.setEnabled(ready)
        self.export_button.setEnabled(ready)

    def _update_cuts(self) -> None:
        cuts = self.controller.applied_cuts(self._media_id) if self.current() else None
        self.use_cuts.setEnabled(bool(cuts))
        self.use_cuts.setText(f"Apply Smart Cut cuts ({len(cuts)} kept parts, {format_duration(kept_duration(cuts))})"
                              if cuts else "Apply Smart Cut cuts (none applied)")

    def _recompute(self) -> None:
        item = self.current()
        self.layout, self.layout_error, self._reframe_path = None, "", None
        warnings: list[str] = []
        if item is None:
            self.layout_error = "Choose a video."
        elif item.metadata is None:
            self.layout_error = "Reading video information…"
        else:
            try:
                stage, canvas, warnings = self.controller.resize_stage(self._media_id, self.settings)
                self.layout = self.controller.resize_layout(self._media_id, self.settings)
                if stage["type"] == "reframe":
                    self._reframe_path = CropPath.from_keyframes(stage["src_w"], stage["src_h"], stage["crop_w"],
                                                                 stage["crop_h"], stage["keyframes"])
            except StudioError as exc:
                self.layout_error = str(exc)
        self._update_info(warnings)
        self._update_cuts()
        self._update_enabled()
        self.canvas.update()
        self.preview.refresh_overlay()

    def _update_info(self, warnings: list[str]) -> None:
        L, item = self.layout, self.current()
        if L is None or item is None:
            self.info.setText("")
            self.warnings.setText(f"⚠ {self.layout_error}" if self.layout_error and item else "")
            return
        meta = item.metadata
        if self.settings.mode == "reframe" and self._reframe_path is not None:
            scale = L.out_w / self._reframe_path.crop_w
        else:
            scale = L.scale
        self.info.setText(
            f"Output: {L.out_w}×{L.out_h} ({aspect_text(L.out_w, L.out_h)}) · source {meta.display_width}×"
            f"{meta.display_height} ({aspect_text(meta.display_width, meta.display_height)})\n"
            f"Scale: {scale:.2f}× · background: {L.background}")
        if self.settings.burn_captions:
            warnings = warnings + ["Captions are rendered at the output size with the video's caption style."]
        self.warnings.setText("\n".join(f"⚠ {w}" for w in warnings))

    def source_rect_at(self, t: float) -> tuple[float, float, float, float] | None:
        if self._reframe_path is not None and self.settings.mode == "reframe":
            x, y = self._reframe_path.at(t)
            return x, y, self._reframe_path.crop_w, self._reframe_path.crop_h
        if self.layout is None:
            return None
        L = self.layout
        return L.src_x, L.src_y, L.src_w, L.src_h

    def _paint_source_overlay(self, painter: QPainter, rect: QRectF, size: QSize) -> None:
        item = self.current()
        src = self.source_rect_at(self.preview.position_seconds())
        if item is None or item.metadata is None or src is None:
            return
        kx, ky = rect.width() / item.metadata.display_width, rect.height() / item.metadata.display_height
        x, y, w, h = src
        painter.setPen(QPen(CROP_LINE, 2))
        painter.drawRect(QRectF(rect.x() + x * kx, rect.y() + y * ky, w * kx, h * ky))

    # ------------------------------------------------------------------ jobs
    def exact_preview(self) -> Job | None:
        if self.current() is None:
            return None
        try:
            job = self.controller.submit_resize_frame(self._media_id, self.preview.position_seconds())
        except StudioError as exc:
            QMessageBox.warning(self, "Exact Frame Preview", str(exc))
            return None
        self._preview_jobs.add(job.id)
        self.export_status.setText("Rendering the frame with FFmpeg…")
        return job

    def export_video(self) -> Job | None:
        item = self.current()
        if item is None or self.layout is None:
            return None
        L = self.layout
        source = Path(item.path)
        suggested = str(source.with_name(f"{source.stem}_{L.out_w}x{L.out_h}.mp4"))
        cuts = self.controller.applied_cuts(self._media_id) if self.settings.use_cuts else None
        notes = [f"Output {L.out_w}×{L.out_h}, mode: {MODES[self.settings.mode].split(' —')[0]}."]
        if cuts:
            notes.append(f"Smart Cut cuts are applied ({format_duration(kept_duration(cuts))}).")
        effects = self.controller.effects(self._media_id).summary()
        if effects:
            notes.append("Speed and audio (Edit page): " + ", ".join(effects) + ".")
        dialog = ExportDialog(self, "Export Resized Video", item.path, suggested, self.services.output_defaults(),
                              captions_available=bool(item.captions and item.captions.segments),
                              captions_checked=self.settings.burn_captions, notes=notes)
        if dialog.exec() != ExportDialog.DialogCode.Accepted:
            return None
        if dialog.burn_captions.isChecked() != self.settings.burn_captions:
            self.burn.setChecked(dialog.burn_captions.isChecked())
        return self.start_export(dialog.output_path(), dialog.form.settings(), dialog.overwrite)

    def start_export(self, output: str, settings, overwrite: bool = False) -> Job | None:
        try:
            job = self.controller.submit_resize_export(self._media_id, output, settings, overwrite=overwrite)
            self.services.remember_output_settings(settings)
        except StudioError as exc:
            QMessageBox.warning(self, "Export", str(exc))
            return None
        self._export_jobs[self._media_id] = job.id
        self._show_export_job()
        return job

    def _export_job(self) -> Job | None:
        job_id = self._export_jobs.get(self._media_id or "")
        try:
            return self.services.jobs.store.get(job_id) if job_id else None
        except KeyError:
            return None

    def _show_export_job(self) -> None:
        job = self._export_job()
        text = ""
        if job is not None:
            name = Path(job.payload["output"]).name
            text = {JobState.QUEUED: f"Export queued: {name}",
                    JobState.RUNNING: f"Exporting {name}… {int(job.progress * 100)}%",
                    JobState.FAILED: f"⚠ Export failed: {job.error}",
                    JobState.CANCELLED: "Export cancelled."}.get(job.state, "")
            if job.state is JobState.COMPLETED and job.result:
                extra = " ⚠ " + " ".join(job.result["warnings"]) if job.result.get("warnings") else ""
                text = f"Exported {job.result['output']} ({job.result['encoder']}).{extra}"
        self.export_status.setText(text)
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
                self.export_status.setText("")
                FramePreviewDialog(job.result["image"], job.result["at"], self, title="Exact Output Frame",
                                   note="Rendered by FFmpeg exactly as the export will crop, scale, place and "
                                        "caption this frame.").exec()
                try:
                    Path(job.result["image"]).unlink(missing_ok=True)
                except OSError:
                    pass
            elif job.state is JobState.FAILED:
                self.export_status.setText(f"⚠ Preview failed: {job.error}")
            return
        if job.kind == RENDER_JOB and job.id == self._export_jobs.get(self._media_id or ""):
            self._show_export_job()

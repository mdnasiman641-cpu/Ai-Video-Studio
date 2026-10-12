"""Edit page: the central editing workspace for one project video.

A compact tool bar above the preview opens each tool's settings in the right-hand panel:
Smart Cut, Captions, Resize / Crop, Auto Reframe, Speed, Audio and Final Export. The panels
(``ui/widgets/edit_tools.py``) edit the video's own saved settings, the same ones the dedicated
pages use, so nothing is kept twice. **Export Final Video** combines every switched-on operation
in one export and one encode; the source file is never modified.
"""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio.captions.style import CaptionStyle
from ai_video_studio.core.errors import StudioError
from ai_video_studio.core.media import MediaItem, MediaStatus, format_duration
from ai_video_studio.jobs.models import Job, JobState
from ai_video_studio.jobs.render_jobs import RENDER_FRAME_JOB
from ai_video_studio.reframe.model import TRACKING_MODES
from ai_video_studio.render.batch import preset_from_edit
from ai_video_studio.render.layout import MODES
from ai_video_studio.render.pipeline import kept_duration
from ai_video_studio.ui.project_controller import JobBridge, ProjectController
from ai_video_studio.ui.widgets.caption_overlay import paint_segments
from ai_video_studio.ui.widgets.common import muted, page_title
from ai_video_studio.ui.widgets.edit_tools import (
    CaptionsPanel,
    GroupPanel,
    ReframePanel,
    ResizePanel,
    SmartCutPanel,
    ToolPanel,
)
from ai_video_studio.ui.widgets.effects_form import EffectsForm
from ai_video_studio.ui.widgets.output_settings import ExportDialog
from ai_video_studio.ui.widgets.video_preview import VideoPreview

TOOLS = [  # (key, label, icon)
    ("smart_cut", "Smart Cut", QStyle.StandardPixmap.SP_MediaSkipForward),
    ("captions", "Captions", QStyle.StandardPixmap.SP_FileDialogDetailedView),
    ("resize", "Resize / Crop", QStyle.StandardPixmap.SP_TitleBarMaxButton),
    ("reframe", "Auto Reframe", QStyle.StandardPixmap.SP_FileDialogContentsView),
    ("speed", "Speed", QStyle.StandardPixmap.SP_MediaSeekForward),
    ("audio", "Audio", QStyle.StandardPixmap.SP_MediaVolume),
    ("final", "Final Export", QStyle.StandardPixmap.SP_DialogSaveButton),
]


class FinalPanel(ToolPanel):
    title = "Final export"

    def __init__(self, page):
        super().__init__(page)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.export_button = QPushButton("Export Final Video…")
        self.export_button.setObjectName("Primary")
        self.bulk_button = QPushButton("Use These Settings in Bulk Processing")
        self.bulk_button.setToolTip("Copy this video's editing settings to the shared settings of Bulk Processing, "
                                    "to apply them to many videos.")
        self.body.addWidget(muted("Operations included in the final export (change them with the tools above):"))
        self.body.addWidget(self.summary)
        self.body.addWidget(self.export_button)
        self.body.addWidget(self.bulk_button)
        self.body.addWidget(self.status)
        self.body.addStretch(1)
        self.export_button.clicked.connect(page.export_final)
        self.bulk_button.clicked.connect(page.send_to_bulk)

    def fill(self, item: MediaItem) -> None:
        self.summary.setText(self.page.summary_html(item))


class EditPage(QWidget):
    bulk_requested = Signal(object)  # RenderPreset built from this video's settings

    def __init__(self, controller: ProjectController, bridge: JobBridge | None = None):
        super().__init__()
        self.controller = controller
        self.captions_page = None  # set by the main window: its dialogs are reused here
        self._current_id: str | None = None
        self._loaded_id: str | None = None  # what the preview player currently has
        self._frame_job: str | None = None

        self.media_combo = QComboBox()
        self.media_combo.setMinimumWidth(280)
        self.preview = VideoPreview()
        self.preview.set_volume(controller.services.settings.get("preview_volume"))
        self.preview.set_overlay(self._paint_overlay)
        self.info_labels = {k: QLabel("-") for k in ("Name", "Duration", "Resolution", "Frame rate", "Audio")}

        # -- tool bar --
        self.tool_buttons: dict[str, QToolButton] = {}
        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        bar = QHBoxLayout()
        bar.setSpacing(4)
        for key, label, icon in TOOLS:
            button = QToolButton()
            button.setText(label)
            button.setIcon(self.style().standardIcon(icon))
            button.setIconSize(QSize(16, 16))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setCheckable(True)
            button.clicked.connect(lambda _c=False, k=key: self.show_tool(k))
            self.tool_group.addButton(button)
            self.tool_buttons[key] = button
            bar.addWidget(button)
        bar.addStretch(1)
        self.export_button = QPushButton("Export Final Video…")
        self.export_button.setObjectName("Primary")
        self.export_button.clicked.connect(self.export_final)
        bar.addWidget(self.export_button)

        # -- tool panels --
        self.effects_form = EffectsForm()
        self.effects_form.changed.connect(self._on_effects_edited)
        self.panels: dict[str, ToolPanel] = {
            "smart_cut": SmartCutPanel(self),
            "captions": CaptionsPanel(self),
            "resize": ResizePanel(self),
            "reframe": ReframePanel(self),
            "speed": GroupPanel(self, "Speed", self.effects_form.speed_group,
                                "Linked audio stays in sync with the picture. The preview plays at the video "
                                "speed; captions follow the speed in the export."),
            "audio": GroupPanel(self, "Audio", self.effects_form.audio_group,
                                "Audio effects are applied in the export (the preview plays the original sound)."),
            "final": FinalPanel(self),
        }
        self.stack = QStackedWidget()
        for panel in self.panels.values():
            scroll = QScrollArea()
            scroll.setWidget(panel)
            scroll.setWidgetResizable(True)
            self.stack.addWidget(scroll)
        self.stack.setMinimumWidth(360)

        top = QHBoxLayout()
        top.addWidget(QLabel("Video:"))
        top.addWidget(self.media_combo)
        for key in ("Duration", "Resolution", "Frame rate", "Audio"):
            top.addSpacing(10)
            top.addWidget(muted(key + ":"))
            top.addWidget(self.info_labels[key])
        top.addStretch(1)

        splitter = QSplitter()
        splitter.addWidget(self.preview)
        splitter.addWidget(self.stack)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        layout = QVBoxLayout(self)
        layout.addWidget(page_title("Edit"))
        layout.addLayout(top)
        layout.addLayout(bar)
        layout.addWidget(splitter, 1)

        self.media_combo.currentIndexChanged.connect(self._on_combo)
        controller.media_list_changed.connect(self.refresh)
        controller.media_updated.connect(self._on_media_updated)
        for signal in (controller.smartcut_changed, controller.resize_changed, controller.captions_changed,
                       controller.effects_changed, controller.reframe_changed):
            signal.connect(self._on_edit_changed)
        controller.smartcut_analysis_finished.connect(lambda media_id, _job: self._on_edit_changed(media_id))
        controller.reframe_analysis_finished.connect(lambda media_id, _job: self._on_edit_changed(media_id))
        controller.services.settings.subscribe(self._on_setting)
        if bridge is not None:
            bridge.job_changed.connect(self._on_job_changed)
        self.show_tool("final")
        self.refresh()

    # ------------------------------------------------------------------ media selection
    def refresh(self) -> None:
        project = self.controller.project
        self.media_combo.blockSignals(True)
        self.media_combo.clear()
        if project:
            for item in project.media:
                self.media_combo.addItem(item.name, item.id)
        self.media_combo.blockSignals(False)
        index = self.media_combo.findData(self._current_id) if self._current_id else -1
        if index < 0 and self.media_combo.count():
            index = 0
        if index >= 0:
            self.media_combo.setCurrentIndex(index)
            self._load(self.media_combo.itemData(index))
        else:
            self._load(None)

    def show_media(self, media_id: str) -> None:
        index = self.media_combo.findData(media_id)
        if index >= 0:
            self.media_combo.setCurrentIndex(index)
            self._load(media_id)

    def current(self) -> MediaItem | None:
        project = self.controller.project
        return project.find_media(self._current_id) if project and self._current_id else None

    def _on_combo(self, index: int) -> None:
        self._load(self.media_combo.itemData(index) if index >= 0 else None)

    def _on_media_updated(self, media_id: str) -> None:
        if media_id == self._current_id:
            self._update_info()
            item = self.current()
            if item and item.metadata:
                self.preview.set_frame_rate(item.metadata.fps)
            self.reload()

    def _on_setting(self, key: str, value) -> None:
        if key == "preview_volume":
            self.preview.set_volume(value)

    def _load(self, media_id: str | None) -> None:
        self._current_id = media_id
        self._update_info()
        self._sync_preview()
        item = self.current()
        if item is not None:
            self.effects_form.set_effects(self.controller.effects(item.id))
        self.reload()

    # ------------------------------------------------------------------ tools
    def show_tool(self, key: str) -> None:
        self.tool_buttons[key].setChecked(True)
        self.stack.setCurrentIndex(list(self.panels).index(key))
        if key == "captions":
            self.panels["captions"].reload_templates()  # presets may have changed on the Captions page
        self.panels[key].load(self.current())

    def current_tool(self) -> str:
        return list(self.panels)[self.stack.currentIndex()]

    def reload(self) -> None:
        """Show the current video's settings in every panel."""
        item = self.current()
        for panel in self.panels.values():
            panel.load(item)
        ok = item is not None and item.metadata is not None and item.status is not MediaStatus.MISSING
        self.export_button.setEnabled(ok)
        self.panels["final"].export_button.setEnabled(ok)
        self.panels["final"].bulk_button.setEnabled(ok)
        speed = self.controller.effects(item.id).video_factor if item is not None else 1.0
        self.preview.player.setPlaybackRate(speed)
        self.preview.refresh_overlay()

    def _on_edit_changed(self, media_id: str) -> None:
        if media_id == self._current_id:
            self.reload()

    def _on_effects_edited(self) -> None:
        item = self.current()
        if item is None:
            return
        try:
            self.controller.set_effects(item.id, self.effects_form.effects())
        except StudioError as exc:
            for key in ("speed", "audio"):
                self.panels[key].status.setText(f"⚠ {exc}")

    def captions_action(self, action: str) -> None:
        """Generate captions or import/export SRT with the Captions page's own dialogs."""
        item = self.current()
        if item is None or self.captions_page is None:
            return
        self.captions_page.show_media(item.id)
        getattr(self.captions_page, action)()

    def _paint_overlay(self, painter: QPainter, rect: QRectF, size: QSize) -> None:
        item = self.current()
        if item is None or item.captions is None or not self.controller.final_operations(item.id)["burn_captions"]:
            return
        paint_segments(painter, rect, item.captions.active_at(self.preview.position_seconds()),
                       item.effective_caption_style)

    # ------------------------------------------------------------------ previews
    def preview_frame(self) -> Job | None:
        item = self.current()
        if item is None:
            return None
        try:
            job = self.controller.submit_final_frame(item.id, self.preview.position_seconds())
        except StudioError as exc:
            QMessageBox.warning(self, "Preview Frame", str(exc))
            return None
        self._frame_job = job.id
        return job

    def _on_job_changed(self, job: Job) -> None:
        if job.kind != RENDER_FRAME_JOB or job.id != self._frame_job or not job.state.is_finished:
            if job.payload.get("media_id") == self._current_id and job.state.is_finished:
                self.reload()  # an analysis finished
            return
        self._frame_job = None
        if job.state is JobState.COMPLETED and job.result:
            from ai_video_studio.ui.caption_dialogs import FramePreviewDialog

            FramePreviewDialog(job.result["image"], job.result["at"], self, title="Final Frame Preview",
                               note="Framing and captions exactly as the final export renders them.").exec()
        elif job.state is JobState.FAILED:
            QMessageBox.warning(self, "Preview Frame", job.error or "The preview could not be rendered.")

    # ------------------------------------------------------------------ final export
    def summary_html(self, item: MediaItem) -> str:
        ops = self.controller.final_operations(item.id)
        rows = []

        def row(on: bool, name: str, detail: str) -> None:
            mark = "✔" if on else "—"
            rows.append(f"{mark} <b>{name}</b>: {detail}")

        cuts = self.controller.applied_cuts(item.id)
        row(ops["use_cuts"], "Smart Cut", f"{len(cuts)} kept part(s), {format_duration(kept_duration(cuts))}"
            if ops["use_cuts"] else ("off" if ops["cuts_available"] else "no cuts applied"))
        row(ops["burn_captions"], "Captions", f"{len(item.captions.segments)} caption(s) burned in"
            if ops["burn_captions"] else ("off" if ops["captions_available"] else "no captions"))
        s = self.controller.resize_settings(item.id)
        if ops["use_resize"]:
            try:
                layout = self.controller.resize_layout(item.id, s)
                size = f"{layout.out_w}×{layout.out_h}"
            except StudioError as exc:
                size = f"⚠ {exc}"
            if ops["reframe"]:
                mode = TRACKING_MODES[self.controller.reframe_state(item.id).settings.tracking_mode].label
                row(True, "Framing", f"Auto Reframe, {size} ({mode})")
            else:
                row(True, "Framing", f"Resize / Crop, {size}, {MODES[s.mode].split(' —')[0]}")
        else:
            row(False, "Framing", "original frame")
        effects = ops["effects"]
        speed = [p for p in effects.summary() if p.startswith(("speed", "video "))]
        audio = [p for p in effects.summary() if p not in speed]
        row(bool(speed), "Speed", speed[0] if speed else "unchanged")
        detail = ", ".join(audio) if audio else "unchanged"
        if audio and not item.metadata.has_audio:
            detail += " ⚠ the video has no audio track"
        row(bool(audio), "Audio", detail)
        return "<br>".join(rows)

    def export_final(self) -> Job | None:
        item = self.current()
        if item is None or item.metadata is None:
            return None
        notes = [re.sub("<[^>]+>", "", self.summary_html(item).replace("<br>", "\n"))]
        source = Path(item.path)
        dialog = ExportDialog(self, "Export Final Video", item.path, str(source.with_name(f"{source.stem}_final.mp4")),
                              self.controller.services.output_defaults(), notes=notes)
        dialog.burn_captions.hide()
        if dialog.exec() != ExportDialog.DialogCode.Accepted:
            return None
        return self.start_export(dialog.output_path(), dialog.form.settings(), dialog.overwrite)

    def start_export(self, output: str, settings, overwrite: bool = False) -> Job | None:
        status = self.panels["final"].status
        try:
            ops = self.controller.final_operations(self._current_id)
            job = self.controller.submit_combined_export(
                self._current_id, output, settings, use_cuts=ops["use_cuts"], use_resize=ops["use_resize"],
                burn_captions=ops["burn_captions"], use_effects=True, overwrite=overwrite)
            self.controller.services.remember_output_settings(settings)
        except StudioError as exc:
            QMessageBox.warning(self, "Export Final Video", str(exc))
            status.setText(f"⚠ {exc}")
            return None
        status.setText(f"Added to the Export Queue: {Path(output).name}")
        return job

    def bulk_preset(self):
        item = self.current()
        ops = self.controller.final_operations(item.id)
        return preset_from_edit(
            item.resize, use_cuts=ops["use_cuts"], smart_cut_settings=self.controller.smartcut_state(item.id).settings,
            burn_captions=bool(item.resize and item.resize.burn_captions), caption_style=item.caption_style or
            CaptionStyle(), effects=ops["effects"],
            tracking_mode=self.controller.reframe_state(item.id).settings.tracking_mode,
            output=self.controller.services.output_defaults())

    def send_to_bulk(self) -> None:
        if self.current() is None:
            return
        try:
            preset = self.bulk_preset()
        except StudioError as exc:
            QMessageBox.warning(self, "Bulk Processing", f"These settings cannot be used for a batch: {exc}")
            return
        self.bulk_requested.emit(preset)

    # ------------------------------------------------------------------ preview player
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._sync_preview()

    def _sync_preview(self) -> None:
        """Load the selected clip into the player, but only while the page is visible."""
        if self._loaded_id == self._current_id:
            return
        # Unloading is always immediate so a closed/removed file is not kept open (locked on Windows).
        if not self.isVisible() and self._current_id is not None:
            return
        self._loaded_id = self._current_id
        item = self.current()
        if item is None or item.status is MediaStatus.MISSING:
            self.preview.load(None)
            if item is not None:
                self.preview.status_label.setText("Source file is missing.")
        else:
            self.preview.load(item.path, item.metadata.fps if item.metadata else None)

    def _update_info(self) -> None:
        item = self.current()
        meta = item.metadata if item else None
        self.info_labels["Name"].setText(item.name if item else "-")
        self.info_labels["Duration"].setText(format_duration(meta.duration) if meta else "-")
        self.info_labels["Resolution"].setText(meta.resolution_text if meta else "-")
        self.info_labels["Frame rate"].setText(f"{meta.fps:g} fps" if meta else "-")
        self.info_labels["Audio"].setText(
            (meta.audio_codec if meta.has_audio else "No audio") if meta else "-"
        )

    def stop_playback(self) -> None:
        self.preview.player.pause()

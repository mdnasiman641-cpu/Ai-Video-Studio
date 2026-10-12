"""Edit page: preview a project video, set its speed and audio effects, and export the final video.

**Export Final Video** combines, in one encode, every operation chosen here: the Smart Cut cuts
(applied on the Smart Cut page), Resize & Crop or Auto Reframe (the Resize & Crop page settings),
burned-in captions (Captions page) and the speed and audio effects set on this page. No
intermediate files are exported and the source file is never modified.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio.core.errors import StudioError
from ai_video_studio.core.media import MediaStatus, format_duration
from ai_video_studio.jobs.models import Job
from ai_video_studio.render.layout import MODES
from ai_video_studio.render.pipeline import kept_duration
from ai_video_studio.ui.project_controller import ProjectController
from ai_video_studio.ui.widgets.common import Card, muted, page_title, section_title
from ai_video_studio.ui.widgets.effects_form import EffectsForm
from ai_video_studio.ui.widgets.output_settings import ExportDialog
from ai_video_studio.ui.widgets.video_preview import VideoPreview


class EditPage(QWidget):
    def __init__(self, controller: ProjectController):
        super().__init__()
        self.controller = controller
        self._current_id: str | None = None
        self._loaded_id: str | None = None  # what the preview player currently has
        self._resize_seen = False  # the video had Resize & Crop settings when the options were last loaded

        self.media_combo = QComboBox()
        self.media_combo.setMinimumWidth(320)
        self.preview = VideoPreview()
        self.preview.set_volume(controller.services.settings.get("preview_volume"))

        info = Card()
        info.body.addWidget(section_title("Clip"))
        form = QFormLayout()
        self.info_labels = {k: QLabel("-") for k in ("Name", "Duration", "Resolution", "Frame rate", "Audio")}
        for key, label in self.info_labels.items():
            label.setWordWrap(True)
            form.addRow(f"{key}:", label)
        info.body.addLayout(form)
        info.body.addSpacing(12)
        info.body.addWidget(section_title("Final export"))
        info.body.addWidget(muted("Choose the operations to include. They are combined into one export; the "
                                  "source file is never modified."))
        self.use_cuts = QCheckBox("Smart Cut cuts")
        self.use_resize = QCheckBox("Resize & Crop / Auto Reframe")
        self.burn_captions = QCheckBox("Burn in captions")
        self.effects_summary = muted("")
        for box in (self.use_cuts, self.use_resize, self.burn_captions):
            box.toggled.connect(self._update_export)
            info.body.addWidget(box)
        info.body.addWidget(self.effects_summary)
        self.export_button = QPushButton("Export Final Video…")
        self.export_button.setObjectName("Primary")
        self.export_button.clicked.connect(self.export_final)
        self.export_status = muted("")
        info.body.addWidget(self.export_button)
        info.body.addWidget(self.export_status)
        info.body.addSpacing(12)
        info.body.addWidget(section_title("Speed and audio"))
        self.effects_form = EffectsForm()
        self.effects_form.changed.connect(self._on_effects_edited)
        self.effects_error = muted("")
        info.body.addWidget(self.effects_form)
        info.body.addWidget(self.effects_error)
        info.body.addWidget(muted("The preview plays at the chosen video speed; audio effects are only heard in "
                                  "the export."))
        info.body.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(info)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(340)

        top = QHBoxLayout()
        top.addWidget(QLabel("Video:"))
        top.addWidget(self.media_combo)
        top.addStretch(1)

        splitter = QSplitter()
        splitter.addWidget(self.preview)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(page_title("Edit"))
        layout.addLayout(top)
        layout.addWidget(splitter, 1)

        self.media_combo.currentIndexChanged.connect(self._on_combo)
        controller.media_list_changed.connect(self.refresh)
        controller.media_updated.connect(self._on_media_updated)
        for signal in (controller.smartcut_changed, controller.resize_changed, controller.captions_changed,
                       controller.effects_changed):
            signal.connect(self._on_edit_changed)
        controller.services.settings.subscribe(self._on_setting)
        self.refresh()

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

    def _on_combo(self, index: int) -> None:
        self._load(self.media_combo.itemData(index) if index >= 0 else None)

    def _on_media_updated(self, media_id: str) -> None:
        if media_id == self._current_id:
            self._update_info()
            project = self.controller.project
            item = project.find_media(media_id) if project else None
            if item and item.metadata:
                self.preview.set_frame_rate(item.metadata.fps)

    def _on_setting(self, key: str, value) -> None:
        if key == "preview_volume":
            self.preview.set_volume(value)

    def _load(self, media_id: str | None) -> None:
        self._current_id = media_id
        self._update_info()
        self._sync_preview()
        self._load_operations()

    def current(self):
        project = self.controller.project
        return project.find_media(self._current_id) if project and self._current_id else None

    # ------------------------------------------------------------------ combined export
    def _on_edit_changed(self, media_id: str) -> None:
        if media_id == self._current_id:
            self._load_operations(keep_choices=True)

    def _load_operations(self, keep_choices: bool = False) -> None:
        item = self.current()
        ok = item is not None and item.metadata is not None and item.status is not MediaStatus.MISSING
        cuts = self.controller.applied_cuts(item.id) if ok else None
        has_captions = bool(ok and item.captions and item.captions.segments)
        was_available = {box: box.isEnabled() for box in (self.use_cuts, self.burn_captions)}
        self.use_cuts.setEnabled(bool(cuts))
        self.use_cuts.setText(f"Smart Cut cuts ({len(cuts)} kept parts, {format_duration(kept_duration(cuts))})"
                              if cuts else "Smart Cut cuts (none applied on the Smart Cut page)")
        resize = self.controller.resize_settings(item.id) if ok else None
        self.use_resize.setEnabled(ok)
        if resize is not None:
            try:
                layout = self.controller.resize_layout(item.id, resize)
                size = f"{layout.out_w}×{layout.out_h}"
            except StudioError:
                size = resize.preset
            self.use_resize.setText(f"Resize & Crop: {size}, {MODES[resize.mode].split(' —')[0]}")
        self.burn_captions.setEnabled(has_captions)
        self.burn_captions.setText(f"Burn in captions ({len(item.captions.segments)}, style from the Captions page)"
                                   if has_captions else "Burn in captions (this video has no captions)")
        if not keep_choices:
            self.use_cuts.setChecked(bool(cuts))
            self.use_resize.setChecked(bool(ok and item.resize is not None))
            self.burn_captions.setChecked(has_captions and bool(resize and resize.burn_captions))
        else:  # an operation that just became available (e.g. cuts applied on the Smart Cut page) is included
            for box, available in ((self.use_cuts, bool(cuts)), (self.burn_captions, has_captions)):
                if available and not was_available[box]:
                    box.setChecked(True)
            if ok and item.resize is not None and not self.use_resize.isChecked() and not self._resize_seen:
                self.use_resize.setChecked(True)
        self._resize_seen = bool(ok and item.resize is not None)
        self.effects_form.setEnabled(ok)
        if ok and not keep_choices:
            self.effects_form.set_effects(self.controller.effects(item.id))
        self.effects_error.setText("")
        self._apply_preview_speed()
        self._update_export()

    def _on_effects_edited(self) -> None:
        item = self.current()
        if item is None:
            return
        try:
            effects = self.effects_form.effects()
            self.controller.set_effects(item.id, effects)
            self.effects_error.setText("")
        except StudioError as exc:
            self.effects_error.setText(f"⚠ {exc}")
        self._apply_preview_speed()
        self._update_export()

    def _apply_preview_speed(self) -> None:
        item = self.current()
        speed = self.controller.effects(item.id).video_factor if item is not None else 1.0
        self.preview.player.setPlaybackRate(speed)

    def _update_export(self, *_args) -> None:
        item = self.current()
        ok = item is not None and item.metadata is not None and item.status is not MediaStatus.MISSING
        summary = self.controller.effects(item.id).summary() if item is not None else []
        self.effects_summary.setText("Speed and audio: " + (", ".join(summary) if summary else "unchanged"))
        self.export_button.setEnabled(ok)

    def chosen_operations(self) -> dict:
        return {"use_cuts": self.use_cuts.isEnabled() and self.use_cuts.isChecked(),
                "use_resize": self.use_resize.isEnabled() and self.use_resize.isChecked(),
                "burn_captions": self.burn_captions.isEnabled() and self.burn_captions.isChecked()}

    def export_final(self) -> Job | None:
        item = self.current()
        if item is None:
            return None
        ops = self.chosen_operations()
        notes = [label for label, on in (("Smart Cut cuts", ops["use_cuts"]), (self.use_resize.text(),
                                         ops["use_resize"]), ("Captions burned in", ops["burn_captions"])) if on]
        summary = self.controller.effects(item.id).summary()
        if summary:
            notes.append("Speed and audio: " + ", ".join(summary))
        source = Path(item.path)
        dialog = ExportDialog(self, "Export Final Video", item.path, str(source.with_name(f"{source.stem}_final.mp4")),
                              self.controller.services.output_defaults(),
                              notes=["Included: " + ("; ".join(notes) if notes else "no editing (re-encode only)")])
        dialog.burn_captions.hide()
        if dialog.exec() != ExportDialog.DialogCode.Accepted:
            return None
        return self.start_export(dialog.output_path(), dialog.form.settings(), dialog.overwrite)

    def start_export(self, output: str, settings, overwrite: bool = False) -> Job | None:
        try:
            job = self.controller.submit_combined_export(self._current_id, output, settings, use_effects=True,
                                                         overwrite=overwrite, **self.chosen_operations())
            self.controller.services.remember_output_settings(settings)
        except StudioError as exc:
            QMessageBox.warning(self, "Export Final Video", str(exc))
            self.export_status.setText(f"⚠ {exc}")
            return None
        self.export_status.setText(f"Added to the Export Queue: {Path(output).name}")
        return job

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
        project = self.controller.project
        item = project.find_media(self._current_id) if project and self._current_id else None
        if item is None or item.status is MediaStatus.MISSING:
            self.preview.load(None)
            if item is not None:
                self.preview.status_label.setText("Source file is missing.")
        else:
            self.preview.load(item.path, item.metadata.fps if item.metadata else None)

    def _update_info(self) -> None:
        project = self.controller.project
        item = project.find_media(self._current_id) if project and self._current_id else None
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

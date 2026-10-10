"""Edit page: choose a project video and preview it.

Editing tools (captions, reframing, cutting, resizing) are added on this page in later phases.
"""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QFormLayout, QHBoxLayout, QLabel, QSplitter, QVBoxLayout, QWidget

from ai_video_studio.core.media import MediaStatus, format_duration
from ai_video_studio.ui.project_controller import ProjectController
from ai_video_studio.ui.widgets.common import Card, muted, page_title, section_title
from ai_video_studio.ui.widgets.video_preview import VideoPreview


class EditPage(QWidget):
    def __init__(self, controller: ProjectController):
        super().__init__()
        self.controller = controller
        self._current_id: str | None = None
        self._loaded_id: str | None = None  # what the preview player currently has

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
        info.body.addWidget(section_title("Tools"))
        info.body.addWidget(muted(
            "Caption, reframing, cutting and resize tools will appear here as they are "
            "implemented (Phases 2–5). The source file is never modified."
        ))
        info.body.addStretch(1)
        info.setMinimumWidth(260)

        top = QHBoxLayout()
        top.addWidget(QLabel("Video:"))
        top.addWidget(self.media_combo)
        top.addStretch(1)

        splitter = QSplitter()
        splitter.addWidget(self.preview)
        splitter.addWidget(info)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(page_title("Edit"))
        layout.addLayout(top)
        layout.addWidget(splitter, 1)

        self.media_combo.currentIndexChanged.connect(self._on_combo)
        controller.media_list_changed.connect(self.refresh)
        controller.media_updated.connect(self._on_media_updated)
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

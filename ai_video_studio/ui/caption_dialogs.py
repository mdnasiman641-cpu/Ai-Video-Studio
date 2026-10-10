"""Dialogs used by the Captions page."""

from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio.captions.correction import word_diff
from ai_video_studio.captions.transcriber import LANGUAGES, MODELS, cuda_device_count
from ai_video_studio.core.media import MediaItem, MediaStatus


class VideoChecklist(QWidget):
    """Checkable list of project videos with select all / none."""

    def __init__(self, media: list[MediaItem], checked_ids: set[str], disable=None):
        super().__init__()
        self.list = QListWidget()
        for item in media:
            reason = disable(item) if disable else None
            row = QListWidgetItem(item.name + (f"  ({reason})" if reason else ""))
            row.setData(Qt.ItemDataRole.UserRole, item.id)
            flags = Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable
            if not reason:
                flags |= Qt.ItemFlag.ItemIsEnabled
            row.setFlags(flags)
            row.setCheckState(Qt.CheckState.Checked if item.id in checked_ids and not reason
                              else Qt.CheckState.Unchecked)
            self.list.addItem(row)
        all_button = QPushButton("Select All")
        none_button = QPushButton("Select None")
        all_button.clicked.connect(lambda: self._set_all(True))
        none_button.clicked.connect(lambda: self._set_all(False))
        buttons = QHBoxLayout()
        buttons.addWidget(all_button)
        buttons.addWidget(none_button)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.list)
        layout.addLayout(buttons)

    def _set_all(self, checked: bool) -> None:
        for i in range(self.list.count()):
            row = self.list.item(i)
            if row.flags() & Qt.ItemFlag.ItemIsEnabled:
                row.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def checked_ids(self) -> list[str]:
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]


def _transcription_block_reason(item: MediaItem) -> str | None:
    if item.status is MediaStatus.MISSING:
        return "file missing"
    if item.metadata is not None and not item.metadata.has_audio:
        return "no audio"
    if item.status is MediaStatus.ERROR:
        return "unreadable"
    return None


class GenerateCaptionsDialog(QDialog):
    def __init__(self, media: list[MediaItem], current_id: str | None, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Generate Captions")
        self.resize(520, 520)
        self.videos = VideoChecklist(media, {current_id} if current_id else set(), _transcription_block_reason)
        self.language = QComboBox()
        for code, name in LANGUAGES:
            self.language.addItem(name, code)
        self.language.setCurrentIndex(max(0, self.language.findData(settings.get("transcription_language"))))
        self.model = QComboBox()
        self.model.addItems(MODELS)
        self.model.setCurrentText(settings.get("whisper_model"))
        self.device = QComboBox()
        for code, name in (("auto", "Automatic (GPU if CUDA works, else CPU)"), ("cuda", "NVIDIA GPU (CUDA)"),
                           ("cpu", "CPU")):
            self.device.addItem(name, code)
        self.device.setCurrentIndex(max(0, self.device.findData(settings.get("transcription_device"))))
        custom = settings.get("whisper_model_dir")
        if custom:
            self.model.setEnabled(False)
            self.model.setToolTip(f"Using the local model folder from Settings: {custom}")

        gpus = cuda_device_count()
        info = QLabel(
            (f"CUDA devices detected: {gpus}. " if gpus else "No CUDA device detected; the CPU will be used. ")
            + "The first run downloads the selected model (tiny ≈75 MB … large-v3 ≈3 GB). "
            "For Bengali, 'medium' or larger gives noticeably better results."
        )
        info.setWordWrap(True)
        info.setObjectName("Muted")
        self.replace_note = QLabel("")
        self.replace_note.setWordWrap(True)
        self.replace_note.setObjectName("Error")
        self._media = {m.id: m for m in media}
        self.videos.list.itemChanged.connect(self._update)

        form = QFormLayout()
        form.addRow("Language:", self.language)
        form.addRow("Model:", self.model)
        form.addRow("Device:", self.device)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Generate")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Videos:"))
        layout.addWidget(self.videos, 1)
        layout.addLayout(form)
        layout.addWidget(info)
        layout.addWidget(self.replace_note)
        layout.addWidget(self.buttons)
        self._update()

    def _update(self, *_args) -> None:
        ids = self.videos.checked_ids()
        existing = [self._media[i].name for i in ids if self._media[i].captions and self._media[i].captions.segments]
        self.replace_note.setText(
            f"Existing captions will be replaced for: {', '.join(existing)}" if existing else "")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(ids))

    def values(self) -> dict:
        return {"media_ids": self.videos.checked_ids(), "language": self.language.currentData(),
                "model": self.model.currentText(), "device": self.device.currentData()}


class ChooseVideosDialog(QDialog):
    def __init__(self, title: str, prompt: str, media: list[MediaItem], checked_ids: set[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(460, 420)
        self.videos = VideoChecklist(media, checked_ids)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(prompt))
        layout.addWidget(self.videos, 1)
        layout.addWidget(buttons)


def diff_html(original: str, suggested: str) -> tuple[str, str]:
    left, right = [], []
    for op, text in word_diff(original, suggested):
        t = html.escape(text)
        if op == "equal":
            left.append(t)
            right.append(t)
        elif op == "delete":
            left.append(f"<span style='background:#5c1f24;color:#ffb3b3;text-decoration:line-through'>{t}</span>")
        else:
            right.append(f"<span style='background:#1f4d2b;color:#b9f5c8'>{t}</span>")
    return " ".join(left), " ".join(right)


class CorrectionReviewDialog(QDialog):
    """Shows each AI suggestion next to the original. Only checked rows are applied."""

    APPLY, DISCARD = 1, 2

    def __init__(self, suggestions: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Review AI Caption Suggestions")
        self.resize(900, 560)
        self.suggestions = suggestions
        self.table = QTableWidget(len(suggestions), 3)
        self.table.setHorizontalHeaderLabels(["Accept", "Original", "Suggested"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for row, sug in enumerate(suggestions):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(Qt.CheckState.Unchecked)
            check.setData(Qt.ItemDataRole.UserRole, sug["id"])
            self.table.setItem(row, 0, check)
            left, right = diff_html(sug["original"], sug["suggested"])
            for col, content in ((1, left), (2, right)):
                label = QLabel(content)
                label.setTextFormat(Qt.TextFormat.RichText)
                label.setWordWrap(True)
                label.setContentsMargins(6, 4, 6, 4)
                self.table.setCellWidget(row, col, label)
        self.table.resizeRowsToContents()
        self.result_action = 0

        accept_all = QPushButton("Check All")
        accept_none = QPushButton("Uncheck All")
        apply_button = QPushButton("Apply Checked")
        apply_button.setObjectName("Primary")
        discard = QPushButton("Discard All Suggestions")
        later = QPushButton("Review Later")
        accept_all.clicked.connect(lambda: self._set_all(True))
        accept_none.clicked.connect(lambda: self._set_all(False))
        apply_button.clicked.connect(lambda: self._finish(self.APPLY))
        discard.clicked.connect(lambda: self._finish(self.DISCARD))
        later.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        for b in (accept_all, accept_none):
            buttons.addWidget(b)
        buttons.addStretch(1)
        for b in (discard, later, apply_button):
            buttons.addWidget(b)
        note = QLabel("Nothing changes until you apply. Captions edited after the check are skipped.")
        note.setObjectName("Muted")
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)

    def _set_all(self, checked: bool) -> None:
        for row in range(self.table.rowCount()):
            self.table.item(row, 0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def set_checked(self, row: int, checked: bool) -> None:
        self.table.item(row, 0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def accepted_ids(self) -> set[str]:
        return {self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in range(self.table.rowCount())
                if self.table.item(r, 0).checkState() == Qt.CheckState.Checked}

    def _finish(self, action: int) -> None:
        self.result_action = action
        self.accept()


class FramePreviewDialog(QDialog):
    def __init__(self, image_path: str, at_seconds: float, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Exact Caption Render at {at_seconds:.2f} s")
        pixmap = QPixmap(image_path)
        label = QLabel()
        label.setPixmap(pixmap.scaled(1280, 720, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation))
        scroll = QScrollArea()
        scroll.setWidget(label)
        scroll.setWidgetResizable(True)
        note = QLabel("Rendered by FFmpeg/libass exactly as it will appear in the exported video.")
        note.setObjectName("Muted")
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(scroll, 1)
        layout.addWidget(close)
        self.resize(min(1320, pixmap.width() + 40), min(820, pixmap.height() + 100))

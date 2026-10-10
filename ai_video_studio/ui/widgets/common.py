"""Small shared widgets and helpers."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget


def page_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("PageTitle")
    return label


def section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("SectionTitle")
    return label


def muted(text: str, wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setObjectName("Muted")
    label.setWordWrap(wrap)
    return label


class Card(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(14, 12, 14, 12)


def open_folder(path: str | Path) -> bool:
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def reveal_in_explorer(path: str | Path) -> bool:
    """Show a file selected in Explorer on Windows; open its folder elsewhere."""
    path = Path(path)
    if sys.platform == "win32" and path.exists():
        subprocess.Popen(["explorer", "/select,", os.path.normpath(str(path))])
        return True
    return open_folder(path.parent)

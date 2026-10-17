"""Toggle switch and settings card used by the Bulk Processing page."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QCheckBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

TEAL = "#1fc8b4"


class ToggleSwitch(QCheckBox):
    """A checkbox drawn as an on/off switch."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(self.sizeHint())

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(40, 22)

    def hitButton(self, pos) -> bool:  # noqa: N802 - the whole switch is clickable
        return self.rect().contains(pos)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isChecked()
        track = QColor(TEAL if on else "#3a3f48")
        if not self.isEnabled():
            track.setAlpha(110)
        r = QRectF(1, 3, self.width() - 2, self.height() - 6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        d = self.height() - 4
        x = self.width() - d - 2 if on else 2
        p.setBrush(QColor("#ffffff" if self.isEnabled() else "#9aa0a8"))
        p.drawEllipse(QRectF(x, 2, d, d))
        p.end()


class ToolCard(QFrame):
    """A titled settings card with an on/off switch; its fields are usable only while it is on."""

    def __init__(self, title: str, subtitle: str, color: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("ToolCard")
        self.setStyleSheet(f"QFrame#ToolCard {{ background-color: #181c22; border: 1px solid {color}; "
                           f"border-radius: 10px; }} QFrame#ToolCard QLabel#CardTitle {{ font-size: 11pt; "
                           f"font-weight: 600; color: #f1f3f6; }}")
        self.toggle = ToggleSwitch()
        name = QLabel(title)
        name.setObjectName("CardTitle")
        sub = QLabel(subtitle)
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        text = QVBoxLayout()
        text.setSpacing(0)
        text.addWidget(name)
        text.addWidget(sub)
        head = QHBoxLayout()
        head.addLayout(text, 1)
        head.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignTop)
        self.body = QWidget()
        self.form = QFormLayout(self.body)
        self.form.setContentsMargins(0, 4, 0, 0)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)  # narrow window: field below label
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.addLayout(head)
        layout.addWidget(self.body)
        layout.addStretch(1)
        self.toggle.toggled.connect(self.body.setEnabled)
        self.body.setEnabled(False)

    def collapse_when_off(self) -> None:
        """Show the card's fields only while it is switched on (a compact list of switched-off cards)."""
        self.body.setVisible(self.toggle.isChecked())
        self.toggle.toggled.connect(self.body.setVisible)

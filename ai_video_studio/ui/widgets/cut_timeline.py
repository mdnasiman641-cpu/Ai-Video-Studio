"""Timeline of a Smart Cut proposal: audio levels, kept and removed segments, threshold, playhead."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ai_video_studio.smartcut.levels import AudioLevels
from ai_video_studio.smartcut.model import CutSegment

KEEP = QColor(46, 125, 80, 110)
REMOVE = QColor(200, 60, 60, 140)
SILENCE_KEPT = QColor(90, 110, 160, 110)
LEVEL = QColor("#d8dee9")
THRESHOLD = QColor("#ffb84d")
SELECTED = QColor("#ffffff")
FLOOR = -60.0  # dB shown at the bottom of the timeline


class CutTimeline(QWidget):
    seek_requested = Signal(float)       # seconds
    segment_clicked = Signal(str)        # segment id

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.levels: AudioLevels | None = None
        self.segments: list[CutSegment] = []
        self.duration = 0.0
        self.threshold = -35.0
        self.position = 0.0
        self.selected: set[str] = set()
        self.setMinimumHeight(90)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip("Green: kept · red: removed · blue: silence kept. Click to move the playhead.")

    def set_data(self, levels: AudioLevels | None, segments: list[CutSegment], duration: float, threshold: float) -> None:
        self.levels, self.segments, self.duration, self.threshold = levels, segments, duration, threshold
        self.update()

    def set_position(self, seconds: float) -> None:
        self.position = seconds
        self.update()

    def set_selected(self, ids: set[str]) -> None:
        self.selected = set(ids)
        self.update()

    def x_for(self, t: float) -> float:
        return 0.0 if self.duration <= 0 else t / self.duration * self.width()

    def time_at(self, x: float) -> float:
        return 0.0 if self.width() <= 0 else max(0.0, min(self.duration, x / self.width() * self.duration))

    def segment_at(self, t: float) -> CutSegment | None:
        return next((s for s in self.segments if s.start <= t < s.end), None)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self.duration <= 0:
            return
        t = self.time_at(event.position().x())
        self.seek_requested.emit(t)
        seg = self.segment_at(t)
        if seg is not None:
            self.segment_clicked.emit(seg.id)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        h, w = self.height(), self.width()
        p.fillRect(self.rect(), QColor("#1b1f27"))
        if self.duration <= 0:
            p.setPen(QColor("#8a93a6"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Analyse the audio to see silences here.")
            p.end()
            return
        for seg in self.segments:
            x0, x1 = self.x_for(seg.start), self.x_for(seg.end)
            color = REMOVE if seg.remove else (SILENCE_KEPT if seg.kind == "silence" else KEEP)
            p.fillRect(QRectF(x0, 0, max(1.0, x1 - x0), h), color)
            if seg.id in self.selected:
                p.setPen(QPen(SELECTED, 2))
                p.drawRect(QRectF(x0 + 1, 1, max(1.0, x1 - x0 - 2), h - 2))

        def y_for(db: float) -> float:
            return h - (max(FLOOR, min(0.0, db)) - FLOOR) / -FLOOR * (h - 4) - 2

        if self.levels and self.levels.peak:
            p.setPen(LEVEL)
            n = len(self.levels.peak)
            for x in range(w):
                i0 = int(x / w * n)
                i1 = max(i0 + 1, int((x + 1) / w * n))
                db = max(self.levels.peak[i0:i1])
                p.drawLine(QPointF(x, h), QPointF(x, y_for(db)))
        p.setPen(QPen(THRESHOLD, 1, Qt.PenStyle.DashLine))
        y = y_for(self.threshold)
        p.drawLine(QPointF(0, y), QPointF(w, y))
        p.setPen(QPen(QColor("#ffffff"), 2))
        x = self.x_for(self.position)
        p.drawLine(QPointF(x, 0), QPointF(x, h))
        p.end()

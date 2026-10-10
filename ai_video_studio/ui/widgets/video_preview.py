"""Video preview with transport controls (Qt Multimedia, FFmpeg backend on Windows)."""

from __future__ import annotations

import logging

from typing import Callable

from PySide6.QtCore import QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio.core.media import format_duration

log = logging.getLogger(__name__)

# overlay(painter, video_rect_on_screen, video_frame_size)
Overlay = Callable[[QPainter, QRectF, QSize], None]


class VideoCanvas(QWidget):
    """Paints decoded frames from a QVideoSink, then an optional overlay (e.g. captions).

    Drawing frames ourselves (instead of QVideoWidget's native surface) lets the caption
    preview be painted on top of the video reliably on every platform.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.sink = QVideoSink(self)
        self.sink.videoFrameChanged.connect(self._on_frame)
        self._image: QImage | None = None
        self.overlay: Overlay | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(320, 180)

    def _on_frame(self, frame: QVideoFrame) -> None:
        if frame.isValid():
            image = frame.toImage()
            if not image.isNull():
                self._image = image
                self.update()

    def clear(self) -> None:
        self._image = None
        self.update()

    def frame_size(self) -> QSize | None:
        return self._image.size() if self._image is not None else None

    def video_rect(self) -> QRectF | None:
        if self._image is None or self._image.width() == 0:
            return None
        iw, ih = self._image.width(), self._image.height()
        scale = min(self.width() / iw, self.height() / ih)
        w, h = iw * scale, ih * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("black"))
        rect = self.video_rect()
        if rect is not None:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(rect, self._image)
            if self.overlay is not None:
                try:
                    self.overlay(painter, rect, self._image.size())
                except Exception:  # noqa: BLE001 - a broken overlay must not break playback
                    log.exception("Video overlay failed")
        painter.end()


class VideoPreview(QWidget):
    position_changed = Signal(int)  # milliseconds

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._fps = 30.0
        self._seeking = False

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.canvas = VideoCanvas(self)
        self.player.setVideoSink(self.canvas.sink)

        style = self.style()
        self.play_button = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay), "")
        self.play_button.setToolTip("Play / Pause (Space)")
        self.stop_button = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_MediaStop), "")
        self.stop_button.setToolTip("Stop")
        self.back_button = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_MediaSeekBackward), "")
        self.back_button.setToolTip("Previous frame (Left)")
        self.forward_button = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_MediaSeekForward), "")
        self.forward_button.setToolTip("Next frame (Right)")
        self.mute_button = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_MediaVolume), "")
        self.mute_button.setCheckable(True)
        self.mute_button.setToolTip("Mute")

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.time_label = QLabel("00:00.0 / 00:00.0")
        self.time_label.setObjectName("Muted")
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(90)
        self.status_label = QLabel("No video loaded")
        self.status_label.setObjectName("Muted")

        controls = QHBoxLayout()
        for button in (self.play_button, self.stop_button, self.back_button, self.forward_button):
            controls.addWidget(button)
        controls.addWidget(self.slider, 1)
        controls.addWidget(self.time_label)
        controls.addWidget(self.mute_button)
        controls.addWidget(self.volume)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.canvas, 1)
        layout.addLayout(controls)
        layout.addWidget(self.status_label)

        self.play_button.clicked.connect(self.toggle_play)
        self.stop_button.clicked.connect(self.stop)
        self.back_button.clicked.connect(lambda: self.step_frames(-1))
        self.forward_button.clicked.connect(lambda: self.step_frames(1))
        self.mute_button.toggled.connect(self._set_muted)
        self.volume.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))
        self.slider.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.slider.sliderReleased.connect(self._end_seek)
        self.slider.sliderMoved.connect(self._preview_seek)

        self.player.durationChanged.connect(self._on_duration)
        self.player.positionChanged.connect(self._on_position)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.errorOccurred.connect(self._on_error)
        self.player.mediaStatusChanged.connect(self._on_media_status)

        for key, slot in (("Space", self.toggle_play), ("Left", lambda: self.step_frames(-1)),
                          ("Right", lambda: self.step_frames(1))):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)

        self.set_volume(80)
        self._update_enabled(False)

    # -- public API ----------------------------------------------------------
    def load(self, path: str | None, fps: float | None = None) -> None:
        self.player.stop()
        self.canvas.clear()
        if not path:
            self.player.setSource(QUrl())
            self.status_label.setText("No video loaded")
            self._update_enabled(False)
            return
        self.set_frame_rate(fps)
        self.player.setSource(QUrl.fromLocalFile(path))
        self.status_label.setText("Loading…")
        self._update_enabled(True)
        log.debug("Preview source set", extra={"path": path})

    def set_frame_rate(self, fps: float | None) -> None:
        """Frame rate used for frame stepping."""
        self._fps = fps if fps and fps > 0 else 30.0

    def set_volume(self, percent: int) -> None:
        self.volume.setValue(percent)
        self.audio.setVolume(percent / 100)

    def toggle_play(self) -> None:
        if self.player.source().isEmpty():
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def stop(self) -> None:
        self.player.stop()

    def step_frames(self, frames: int) -> None:
        if self.player.source().isEmpty():
            return
        self.player.pause()
        step_ms = 1000.0 / self._fps
        target = int(round(self.player.position() + frames * step_ms))
        self.player.setPosition(max(0, min(target, self.player.duration())))

    def seek(self, ms: int) -> None:
        self.player.setPosition(ms)

    def position_seconds(self) -> float:
        return self.player.position() / 1000

    def set_overlay(self, overlay: Overlay | None) -> None:
        self.canvas.overlay = overlay
        self.canvas.update()

    def refresh_overlay(self) -> None:
        self.canvas.update()

    # -- slots ---------------------------------------------------------------
    def _set_muted(self, muted: bool) -> None:
        self.audio.setMuted(muted)
        icon = QStyle.StandardPixmap.SP_MediaVolumeMuted if muted else QStyle.StandardPixmap.SP_MediaVolume
        self.mute_button.setIcon(self.style().standardIcon(icon))

    def _end_seek(self) -> None:
        self._seeking = False
        self.player.setPosition(self.slider.value())

    def _preview_seek(self, value: int) -> None:
        self.player.setPosition(value)
        self._update_time(value)

    def _on_duration(self, duration: int) -> None:
        self.slider.setRange(0, max(0, duration))
        self._update_time(self.player.position())

    def _on_position(self, position: int) -> None:
        if not self._seeking:
            self.slider.setValue(position)
        self._update_time(position)
        self.position_changed.emit(position)

    def _update_time(self, position: int) -> None:
        self.time_label.setText(
            f"{format_duration(position / 1000)} / {format_duration(self.player.duration() / 1000)}"
        )

    def _on_state(self, state: QMediaPlayer.PlaybackState) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        icon = QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay
        self.play_button.setIcon(self.style().standardIcon(icon))

    def _on_media_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            self.status_label.setText("Ready")
            if self.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState:
                # Pausing from the stopped state decodes and shows the first frame.
                self.player.pause()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self.status_label.setText("This video cannot be previewed.")

    def _on_error(self, error, message: str) -> None:
        if error != QMediaPlayer.Error.NoError:
            log.warning("Preview error", extra={"error": str(error), "detail": message})
            self.status_label.setText(f"Preview error: {message or error}")

    def _update_enabled(self, enabled: bool) -> None:
        for w in (self.play_button, self.stop_button, self.back_button, self.forward_button, self.slider):
            w.setEnabled(enabled)

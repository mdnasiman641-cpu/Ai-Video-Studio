"""GUI tests for the AI Auto Reframe page (offscreen, real FFmpeg, colour-threshold detector)."""

import time

import pytest
from PySide6.QtCore import QRectF, QSize
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QFileDialog, QMessageBox

from ai_video_studio.core.ffmpeg_tools import find_tool, probe_video
from ai_video_studio.jobs.models import JobState
from ai_video_studio.jobs.reframe_jobs import REFRAME_ANALYZE_JOB, make_reframe_analyze_handler
from ai_video_studio.reframe.detection import DetectorSet
from ai_video_studio.reframe.model import ReframeState
from ai_video_studio.services import Services
from ai_video_studio.ui.main_window import MainWindow
from tests.conftest import requires_ffmpeg
from tests.integration.test_reframe_jobs_real import red_detector
from tests.reframe_helpers import face_at, make_analysis, make_track

pytestmark = requires_ffmpeg


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch, square_clip):
    for name in ("warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    services = Services(tmp_path / "ui.db")
    services.settings.set("video_encoder", "libx264")
    services.jobs.register(REFRAME_ANALYZE_JOB, make_reframe_analyze_handler(
        services.ffmpeg_path, lambda p: DetectorSet(red_detector, None, "red test detector", [])))
    win = MainWindow(services)
    win.show()
    win.media_page.import_paths([str(square_clip)])
    qtbot.waitUntil(lambda: win.controller.project.media[0].metadata is not None, timeout=20000)
    win.navigate("Auto Reframe")
    yield win
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Discard)
    win.close()
    win.deleteLater()
    services.close()


def media(win):
    return win.controller.project.media[0]


def analyse(qtbot, win):
    page = win.reframe_page
    assert page.analyze_button.isEnabled()
    page.analyze()
    qtbot.waitUntil(lambda: media(win).reframe is not None and media(win).reframe.analysis is not None,
                    timeout=30000)
    qtbot.waitUntil(lambda: "Analysis finished" in page.status.text(), timeout=5000)
    return page


def test_page_replaces_the_planned_placeholder(window):
    page = window.reframe_page
    assert window.pages.currentWidget() is page
    assert page.analysis_info.text() == "Not analysed yet."
    assert "CPU" in page.hardware.text() and "NVENC" in page.hardware.text()
    assert page.warnings.text() and "Analyse" in page.warnings.text()  # tracking needs an analysis first
    assert not page.export_button.isEnabled()


def test_analyse_then_preview_shows_tracked_crop(qtbot, window):
    page = analyse(qtbot, window)
    assert page.subjects.count() == 2  # automatic + one subject
    assert "#1 Face" in page.subjects.item(1).text()
    assert page._path is not None and not page._path.is_static
    assert page.export_button.isEnabled() and "1080×1920" in page.output_info.text()

    # Source overlay: crop rectangle and the subject's box are drawn.
    image = QImage(640, 360, QImage.Format.Format_RGB32)
    image.fill(QColor("black"))
    painter = QPainter(image)
    page._paint_source_overlay(painter, QRectF(0, 0, 640, 360), QSize(640, 360))
    painter.end()
    x, _y = page._path.at(page.preview.position_seconds())
    border = [QColor(image.pixel(c, 180)).lightness() for c in range(max(0, x - 2), x + 3)]
    assert max(border) > 200  # white crop border

    # Output preview: shows the crop region of the current frame.
    frame = QImage(640, 360, QImage.Format.Format_RGB32)
    frame.fill(QColor("blue"))
    page.preview.canvas._image = frame
    grabbed = page.output.grab().toImage()
    centre = QColor(grabbed.pixel(grabbed.width() // 2, grabbed.height() // 2))
    assert centre.blue() > 200 and centre.red() < 50


def test_controls_update_settings_and_reset(qtbot, window):
    page = window.reframe_page
    page.preset.setCurrentIndex(page.preset.findData("1:1"))
    settings = window.controller.reframe_state(media(window).id).settings
    assert settings.preset == "1:1" and window.controller.project.dirty
    assert page.size_combo.itemText(0) == "1080 × 1080"
    page.size_combo.setCurrentIndex(1)
    page.zoom.setValue(1.5)
    page.offset_x.setValue(10)
    page.caption_safe.setCurrentIndex(page.caption_safe.findData("top"))
    s = window.controller.reframe_state(media(window).id).settings
    assert (s.output_width, s.output_height) == (720, 720) and s.zoom == 1.5 and s.offset_x == pytest.approx(0.1)
    assert s.caption_safe == "top"
    page.reset_framing()
    s = window.controller.reframe_state(media(window).id).settings
    assert s.zoom == 1.0 and s.offset_x == 0 and s.preset == "1:1" and s.caption_safe == "top"
    assert page.zoom.value() == 1.0


def test_centre_crop_mode_works_without_analysis(qtbot, window, tmp_path):
    page = window.reframe_page
    page.mode.setCurrentIndex(page.mode.findData("center"))
    qtbot.waitUntil(lambda: page._path is not None, timeout=3000)
    assert page._path.is_static and page.export_button.isEnabled()
    assert not page.subjects.isEnabled()


def test_subject_choice_and_changes(qtbot, window, monkeypatch):
    page = window.reframe_page
    item = media(window)
    analysis = make_analysis([make_track(1, "face", face_at(100, 150, 60), 3, w=640, h=360),
                              make_track(2, "face", face_at(500, 150, 40), 3, w=640, h=360)],
                             duration=3, w=640, h=360)
    item.reframe = ReframeState(analysis=analysis)
    page._load_controls()
    page.recompute()
    labels = [page.subjects.item(i).text() for i in range(page.subjects.count())]
    assert labels[0].startswith("● Automatic") and len(labels) == 3
    page.subjects.setCurrentRow([i for i, t in enumerate(labels) if "#2" in t][0])
    page.follow_selected_from_start()
    assert window.controller.reframe_state(item.id).settings.target == 2
    qtbot.waitUntil(lambda: page._path is not None and page._path.followed_at(1) == 2, timeout=3000)

    monkeypatch.setattr(page.preview, "position_seconds", lambda: 1.5)
    page.subjects.setCurrentRow([i for i in range(page.subjects.count()) if "#1" in page.subjects.item(i).text()][0])
    page.follow_selected_from_playhead()
    s = window.controller.reframe_state(item.id).settings
    assert s.target == 2 and s.switches == [[1.5, 1]]
    assert "01.5" in page.switches_info.text()
    qtbot.waitUntil(lambda: page._path.followed_at(2.8) == 1, timeout=3000)
    page.clear_switches()
    assert window.controller.reframe_state(item.id).settings.switches == []


def test_no_subjects_warns_and_blocks_tracking_export(qtbot, window):
    page = window.reframe_page
    item = media(window)
    item.reframe = ReframeState(analysis=make_analysis([], duration=3, w=640, h=360))
    item.reframe.analysis.warnings = ["No faces or people were detected."]
    page.recompute()
    assert "No faces" in page.warnings.text()
    assert not page.export_button.isEnabled()


def test_export_from_page(qtbot, window, tmp_path, monkeypatch):
    page = analyse(qtbot, window)
    page.size_combo.setCurrentIndex(1)  # 720 x 1280
    out = tmp_path / "reels.mp4"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    page.export_video()
    qtbot.waitUntil(lambda: "Exported" in page.status.text() or "failed" in page.status.text(), timeout=60000)
    assert "Exported" in page.status.text(), page.status.text()
    meta = probe_video(out, find_tool("ffprobe"))
    assert (meta.width, meta.height) == (720, 1280) and meta.has_audio
    assert page.start_export(media(window).path) is None  # never overwrites the source


def test_export_asks_before_exporting_with_warnings(qtbot, window, tmp_path, monkeypatch):
    page = window.reframe_page
    item = media(window)
    track = make_track(1, "face", lambda t: face_at(100, 150, 60)(t) if t < 0.5 else None, 3, w=640, h=360)
    item.reframe = ReframeState(analysis=make_analysis([track], duration=3, w=640, h=360))
    page.recompute()
    assert "only" in page.warnings.text()
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a) or QMessageBox.StandardButton.No)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: pytest.fail("export must not continue"))
    page.export_video()
    assert asked and "only" in asked[0][2]


def test_reframe_data_survives_save_and_reopen(qtbot, window, tmp_path):
    page = analyse(qtbot, window)
    page.preset.setCurrentIndex(page.preset.findData("4:5"))
    path = tmp_path / "reframe.avsproj"
    assert window._save(str(path))
    window.controller.close_project()
    window.open_project(str(path), confirmed=True)
    window.navigate("Auto Reframe")
    item = media(window)
    assert item.reframe.settings.preset == "4:5" and len(item.reframe.analysis.tracks) == 1
    assert window.reframe_page.subjects.count() == 2
    assert window.reframe_page.preset.currentData() == "4:5"


def test_cancel_analysis(qtbot, window):
    def slow(frame):
        time.sleep(0.3)
        return []

    window.services.jobs.register(REFRAME_ANALYZE_JOB, make_reframe_analyze_handler(
        window.services.ffmpeg_path, lambda p: DetectorSet(slow, None, "slow", [])))
    page = window.reframe_page
    page.tracking.spins["analysis_fps"][0].setValue(15)
    job = page.analyze()
    qtbot.waitUntil(lambda: page.cancel_button.isEnabled(), timeout=5000)
    page.cancel_analysis()
    qtbot.waitUntil(lambda: window.services.jobs.store.get(job.id).state is JobState.CANCELLED, timeout=20000)
    qtbot.waitUntil(lambda: "cancelled" in page.status.text(), timeout=5000)
    assert media(window).reframe.analysis is None

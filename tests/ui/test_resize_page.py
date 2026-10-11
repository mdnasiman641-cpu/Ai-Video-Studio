"""GUI tests for the Resize & Crop page (offscreen, real FFmpeg)."""

import pytest
from PySide6.QtWidgets import QMessageBox

from ai_video_studio.captions.model import CaptionSegment, CaptionTrack
from ai_video_studio.core.ffmpeg_tools import find_tool, probe_video
from ai_video_studio.jobs.models import JobState
from ai_video_studio.render.output import OutputSettings
from ai_video_studio.services import Services
from ai_video_studio.storage.project import Project
from ai_video_studio.ui import caption_dialogs
from ai_video_studio.ui.main_window import MainWindow
from tests.conftest import requires_ffmpeg

pytestmark = requires_ffmpeg


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch, square_clip):
    for name in ("warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    services = Services(tmp_path / "ui.db")
    win = MainWindow(services)
    win.show()
    win.media_page.import_paths([str(square_clip)])
    qtbot.waitUntil(lambda: win.controller.project.media[0].metadata is not None, timeout=20000)
    win.navigate("Resize & Crop")
    yield win
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Discard)
    win.close()
    win.deleteLater()
    services.close()


def select(combo, key):
    combo.setCurrentIndex(combo.findData(key))


def test_defaults_and_live_layout(window):
    page = window.resize_page
    assert window.pages.currentWidget() is page
    assert page.layout is not None and (page.layout.out_w, page.layout.out_h) == (1080, 1920)
    assert "1080×1920 (9:16)" in page.info.text() and "blur" in page.info.text()
    assert page.export_button.isEnabled()
    select(page.preset, "1080x1080")
    select(page.mode, "fill")
    item = window.controller.project.media[0]
    assert item.resize.preset == "1080x1080" and item.resize.mode == "fill"
    assert page.layout.background == "none" and page.layout.src_h == 360
    assert page.pos_x.isEnabled() and not page.zoom.isEnabled()


def test_invalid_custom_size_and_reframe_without_analysis(window):
    page = window.resize_page
    select(page.preset, "custom")
    page.width_spin.setValue(1001)
    assert page.layout is None or "even" in page.warnings.text()
    assert "even" in page.warnings.text()
    page.width_spin.setValue(1000)
    assert page.layout is not None and page.layout.out_w == 1000
    select(page.preset, "1080x1920")
    select(page.mode, "reframe")
    assert page.layout is None and "Auto Reframe" in page.layout_error
    assert not page.export_button.isEnabled()


def test_canvas_draws_the_frame_and_captions(qtbot, window):
    page = window.resize_page
    item = window.controller.project.media[0]
    window.controller.set_captions(item.id, CaptionTrack([CaptionSegment(0.0, 3.0, "Caption on canvas")]))
    page.burn.setChecked(True)
    assert item.resize.burn_captions
    qtbot.waitUntil(lambda: page.preview.canvas.image() is not None, timeout=10000)
    image = page.canvas.grab().toImage()
    assert not image.isNull()
    colours = {image.pixel(x, y) for x in range(0, image.width(), 7) for y in range(0, image.height(), 7)}
    assert len(colours) > 5  # background, video and captions were painted


def test_exact_preview_export_and_reopen(qtbot, window, tmp_path, monkeypatch):
    page = window.resize_page
    monkeypatch.setattr(caption_dialogs.FramePreviewDialog, "exec", lambda self: 0)
    select(page.preset, "custom")
    page.width_spin.setValue(320)
    page.height_spin.setValue(320)
    select(page.mode, "fit")
    job = page.exact_preview()
    qtbot.waitUntil(lambda: window.services.jobs.store.get(job.id).state.is_finished, timeout=30000)
    assert window.services.jobs.store.get(job.id).state is JobState.COMPLETED
    output = tmp_path / "square.mp4"
    job = page.start_export(str(output), OutputSettings(encoder="cpu", quality="small"))
    qtbot.waitUntil(lambda: window.services.jobs.store.get(job.id).state.is_finished, timeout=60000)
    done = window.services.jobs.store.get(job.id)
    assert done.state is JobState.COMPLETED, done.error
    meta = probe_video(str(output), find_tool("ffprobe"))
    assert (meta.width, meta.height) == (320, 320) and meta.has_audio
    qtbot.waitUntil(lambda: page.open_folder_button.isEnabled(), timeout=5000)
    saved = window.controller.save_project(tmp_path / "r.avsproj")
    resize = Project.load(saved).media[0].resize
    assert (resize.preset, resize.width, resize.mode) == ("custom", 320, "fit")


def test_auto_reframe_mode_uses_the_stored_analysis(window):
    from ai_video_studio.reframe.model import ReframeState
    from tests.reframe_helpers import make_analysis, make_track

    page = window.resize_page
    item = window.controller.project.media[0]
    track = make_track(1, "face", lambda t: (100 + 100 * t, 150, 50, 60), 3.0, w=640, h=360)
    item.reframe = ReframeState(window.controller.default_reframe_settings(), make_analysis([track], 3.0, w=640, h=360))
    select(page.preset, "1080x1920")
    select(page.mode, "reframe")
    assert page.layout is not None and page._reframe_path is not None
    stage, canvas, _notes = window.controller.resize_stage(item.id)
    assert stage["type"] == "reframe" and canvas == (1080, 1920)
    x_start, _ = page._reframe_path.at(0.2)
    x_end, _ = page._reframe_path.at(2.8)
    assert x_end > x_start  # the crop follows the face to the right
    select(page.preset, "custom")
    page.width_spin.setValue(1000)
    page.height_spin.setValue(700)
    assert page.layout is None and "16:9, 9:16, 1:1 and 4:5" in page.layout_error

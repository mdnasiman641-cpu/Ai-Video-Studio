"""GUI tests for tracking modes: Settings defaults, per-video mode, reanalysis and visible controls."""

import pytest
from PySide6.QtWidgets import QMessageBox

from ai_video_studio.reframe.model import ReframeState
from ai_video_studio.services import Services
from ai_video_studio.ui.main_window import MainWindow
from tests.conftest import requires_ffmpeg
from tests.reframe_helpers import face_at, make_analysis, make_track

pytestmark = requires_ffmpeg


@pytest.fixture
def make_window(qtbot, tmp_path, monkeypatch):
    for name in ("warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Discard)
    opened = []

    def make(db_name="ui.db"):
        services = Services(tmp_path / db_name)
        win = MainWindow(services)
        win.show()
        opened.append((win, services))
        return win

    yield make
    for win, services in opened:
        win.close()
        win.deleteLater()
        services.close()


def with_clip(qtbot, win, clip):
    win.media_page.import_paths([str(clip)])
    qtbot.waitUntil(lambda: win.controller.project.media[0].metadata is not None, timeout=20000)
    win.navigate("Auto Reframe")
    return win.reframe_page, win.controller.project.media[0]


def test_settings_tracking_section_persists_after_restart(make_window):
    win = make_window()
    page = win.settings_page
    page.tracking.mode.setCurrentIndex(page.tracking.mode.findData("active_speaker"))
    page.tracking.spins["switch_seconds"][0].setValue(2.0)
    page.tracking.spins["max_upscale"][0].setValue(1.5)
    page.tracking.fallback.setCurrentIndex(page.tracking.fallback.findData("group"))
    assert page.save()
    win.close()
    win.services.close()

    restarted = make_window()  # same database: like starting the app again
    s = restarted.services.settings
    assert s.get("reframe_tracking_mode") == "active_speaker"
    assert s.get("reframe_switch_seconds") == 2.0 and s.get("reframe_max_upscale") == 1.5
    assert s.get("reframe_fallback") == "group"
    assert restarted.settings_page.tracking.current_mode() == "active_speaker"
    defaults = restarted.controller.default_reframe_settings()
    assert defaults.tracking_mode == "active_speaker" and defaults.switch_seconds == 2.0


def test_settings_show_only_options_the_mode_uses(make_window):
    tracking = make_window().settings_page.tracking

    def shown(name):
        return not tracking.spins[name][0].isHidden()

    tracking.mode.setCurrentIndex(tracking.mode.findData("auto_face"))
    assert shown("confidence_threshold") and not shown("speech_threshold") and not shown("switch_seconds")
    tracking.mode.setCurrentIndex(tracking.mode.findData("active_speaker"))
    assert shown("speech_threshold") and shown("switch_seconds") and shown("av_fps")
    tracking.mode.setCurrentIndex(tracking.mode.findData("speech_audio"))
    assert shown("speech_threshold") and not shown("switch_seconds") and not shown("av_fps")
    tracking.mode.setCurrentIndex(tracking.mode.findData("center"))
    assert not shown("hold_seconds") and not shown("smoothing") and tracking.fallback.isHidden()
    assert shown("max_upscale")
    tracking.mode.setCurrentIndex(tracking.mode.findData("speech_audio"))
    assert "Audio alone cannot tell" in tracking.description.text()


def test_new_videos_start_with_the_default_mode(qtbot, make_window, square_clip):
    win = make_window()
    win.services.settings.set("reframe_tracking_mode", "speech_audio")
    page, item = with_clip(qtbot, win, square_clip)
    assert page.tracking.current_mode() == "speech_audio"
    assert win.controller.reframe_state(item.id).settings.tracking_mode == "speech_audio"


def test_changing_mode_keeps_results_and_asks_for_reanalysis(qtbot, make_window, square_clip):
    win = make_window()
    page, item = with_clip(qtbot, win, square_clip)
    analysis = make_analysis([make_track(1, "face", face_at(200, 150, 60), 3, w=640, h=360)], duration=3,
                             w=640, h=360)
    item.reframe = ReframeState(win.controller.default_reframe_settings(), analysis)
    page._load_controls()
    page.recompute()
    assert page.reanalysis.isHidden() and page._path is not None

    page.mode.setCurrentIndex(page.mode.findData("active_speaker"))
    qtbot.waitUntil(lambda: page._path is None, timeout=3000)
    assert "Reanalysis required" in page.reanalysis.text() and "speech" in page.reanalysis.text()
    assert item.reframe.analysis is analysis           # stored results are kept
    assert not page.export_button.isEnabled()          # never exported with results for another mode
    assert page.subject_group.isHidden()               # active speaker chooses the subject itself

    page.mode.setCurrentIndex(page.mode.findData("auto_face"))
    qtbot.waitUntil(lambda: page._path is not None, timeout=3000)
    assert page.reanalysis.isHidden() and not page.subject_group.isHidden()
    assert "Auto Face Tracking" in page.live_status.text() and "upscale" in page.live_status.text()


def test_analysis_payload_matches_the_mode(qtbot, make_window, square_clip):
    win = make_window()
    page, item = with_clip(qtbot, win, square_clip)
    win.services.jobs.pause()  # inspect the payload without running the job
    page.mode.setCurrentIndex(page.mode.findData("active_speaker"))
    job = win.controller.submit_reframe_analysis(item.id)
    assert job.payload["features"] == ["faces", "mouth", "speech"] and job.payload["sample_fps"] == 15.0
    win.services.jobs.cancel(job.id)
    page.mode.setCurrentIndex(page.mode.findData("auto_face"))
    job = win.controller.submit_reframe_analysis(item.id)
    assert "speech" not in job.payload["features"] and job.payload["sample_fps"] == 5.0
    win.services.jobs.cancel(job.id)


def test_mode_is_saved_in_the_project(qtbot, make_window, square_clip, tmp_path):
    win = make_window()
    page, item = with_clip(qtbot, win, square_clip)
    page.mode.setCurrentIndex(page.mode.findData("manual"))
    path = tmp_path / "modes.avsproj"
    assert win._save(str(path))
    win.services.settings.set("reframe_tracking_mode", "center")  # a different default must not leak in
    win.controller.close_project()
    win.open_project(str(path), confirmed=True)
    win.navigate("Auto Reframe")
    assert win.controller.project.media[0].reframe.settings.tracking_mode == "manual"
    assert win.reframe_page.tracking.current_mode() == "manual"


def test_speech_modes_refuse_videos_without_audio(qtbot, make_window, tmp_path):
    import subprocess

    clip = tmp_path / "silent.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=320x180:r=25:d=1",
                    "-c:v", "libx264", str(clip)], check=True)
    win = make_window()
    page, item = with_clip(qtbot, win, clip)
    page.mode.setCurrentIndex(page.mode.findData("speech_audio"))
    from ai_video_studio.core.errors import StudioError

    with pytest.raises(StudioError, match="no audio track"):
        win.controller.submit_reframe_analysis(item.id)

"""GUI tests for the Bulk Processing page (offscreen, real FFmpeg)."""

import pytest
from PySide6.QtWidgets import QInputDialog, QMessageBox

from ai_video_studio.core.ffmpeg_tools import find_tool, probe_video
from ai_video_studio.render.batch import RenderPreset
from ai_video_studio.render.layout import ResizeSettings
from ai_video_studio.render.output import OutputSettings
from ai_video_studio.services import Services
from ai_video_studio.ui.main_window import MainWindow
from ai_video_studio.ui.pages.bulk import COL_RESOLUTION, COL_SETTINGS, COL_STATUS, BulkPage
from tests.conftest import make_sample_video, requires_ffmpeg

pytestmark = requires_ffmpeg


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    for name in ("warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    services = Services(tmp_path / "ui.db")
    win = MainWindow(services)
    win.show()
    win.navigate("Bulk Processing")
    yield win
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Discard)
    win.close()
    win.deleteLater()
    services.close()


@pytest.fixture
def videos(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    return [make_sample_video(folder / "one.mp4", seconds=1.0), make_sample_video(folder / "two.mp4", seconds=1.0,
                                                                                 audio=False)]


def small_preset(**kw) -> RenderPreset:
    return RenderPreset(resize=ResizeSettings(preset="custom", width=160, height=160, mode="blur"),
                        output=OutputSettings(encoder="cpu", quality="small"), **kw)


def statuses(page):
    return [page.table.item(r, COL_STATUS).text() for r in range(page.table.rowCount())]


def test_add_videos_status_and_settings(qtbot, window, videos, tmp_path):
    page = window.bulk_page
    junk = tmp_path / "notes.txt"
    junk.write_text("x")
    added, duplicates, rejected = page.add_paths([str(videos[0]), str(videos[0]), str(junk)])
    assert (added, duplicates, len(rejected)) == (1, 1, 1)
    page.add_paths([str(videos[0].parent)])  # a folder: the second video is added, the first is known
    assert len(page.batch.items) == 2
    qtbot.waitUntil(lambda: statuses(page) == ["Ready", "Ready"], timeout=20000)
    assert "→ 1080×1920" in page.table.item(0, COL_RESOLUTION).text()  # default shared settings
    assert page.start_button.isEnabled()
    page.editor.set_preset(small_preset())
    page._on_shared_changed()
    assert page.batch.shared.resize.width == 160
    page.batch.items[1].override = small_preset(filename_template="{original_name}_custom")
    page.refresh()
    assert [page.table.item(r, COL_SETTINGS).text() for r in range(2)] == ["Shared", "Own"]
    page.output_dir.setText("")
    page._on_output_changed()
    assert page.start() == []  # no output folder


def test_start_pause_resume_cancel_retry_and_conflicts(qtbot, window, videos, tmp_path):
    page = window.bulk_page
    out = tmp_path / "out"
    out.mkdir()
    (out / "one_160x160.mp4").write_bytes(b"existing")  # must not be overwritten
    page.add_paths([str(v) for v in videos])
    qtbot.waitUntil(lambda: statuses(page) == ["Ready", "Ready"], timeout=20000)
    page.editor.set_preset(small_preset())
    page._on_shared_changed()
    page.output_dir.setText(str(out))
    page._on_output_changed()
    window.services.jobs.pause()  # nothing starts until the test says so
    jobs = page.start()
    assert len(jobs) == 2 and statuses(page) == ["Waiting", "Waiting"]
    assert page.batch.items[0].output.endswith("one_160x160 (2).mp4")
    assert "already exists" in page.details(page.batch.items[0])
    page.pause()
    assert statuses(page) == ["Waiting (paused)", "Waiting (paused)"]
    page.cancel_items([page.batch.items[1]])
    assert statuses(page)[1] == "Cancelled"
    page.resume()
    window.services.jobs.resume()
    qtbot.waitUntil(lambda: statuses(page)[0] == "Completed", timeout=60000)
    page.retry_failed()
    qtbot.waitUntil(lambda: statuses(page) == ["Completed", "Completed"], timeout=60000)
    assert (out / "one_160x160.mp4").read_bytes() == b"existing"
    meta = probe_video(str(out / "one_160x160 (2).mp4"), find_tool("ffprobe"))
    assert (meta.width, meta.height) == (160, 160)
    assert page.overall.value() == 100
    # The batch (with job links) is restored by a new page, e.g. after a restart.
    again = BulkPage(window.controller, window.bridge)
    assert [i.output for i in again.batch.items] == [i.output for i in page.batch.items]
    assert [again.table.item(r, COL_STATUS).text() for r in range(2)] == ["Completed", "Completed"]
    again.deleteLater()


def test_presets_save_rename_delete(window, monkeypatch):
    page = window.bulk_page
    page.editor.set_preset(small_preset())
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("My reels", True))
    page.save_preset()
    assert page.selected_preset() == "My reels" and page.update_preset_button.isEnabled()
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Renamed reels", True))
    page.rename_preset()
    assert window.services.render_presets.exists("Renamed reels")
    page.preset_combo.setCurrentIndex(page.preset_combo.findData("YouTube 1920x1080"))
    assert not page.delete_preset_button.isEnabled()  # built-in presets are protected
    page.apply_preset()
    assert page.batch.shared.resize.preset == "1920x1080" and page.batch.preset_name == "YouTube 1920x1080"
    page.preset_combo.setCurrentIndex(page.preset_combo.findData("Renamed reels"))
    page.delete_preset()
    assert not window.services.render_presets.exists("Renamed reels")

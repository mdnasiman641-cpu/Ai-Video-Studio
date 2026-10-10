"""GUI tests (offscreen) covering the Phase 1 workflows end to end."""

import pytest
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QMessageBox

from ai_video_studio.core.media import MediaStatus
from ai_video_studio.jobs.models import JobState
from ai_video_studio.services import Services
from ai_video_studio.storage.settings import SETTINGS
from ai_video_studio.ui.main_window import NAVIGATION, MainWindow
from tests.conftest import requires_ffmpeg


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    # Never block on modal dialogs in tests.
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Discard)
    services = Services(tmp_path / "ui.db")
    win = MainWindow(services)
    yield win
    win.close()
    win.deleteLater()
    services.close()


def _wait_ready(qtbot, win, count):
    def ready():
        media = win.controller.project.media
        assert len(media) == count
        assert all(m.status is not MediaStatus.PENDING for m in media)
    qtbot.waitUntil(ready, timeout=20000)


def test_all_navigation_pages(window):
    for row, (name, _icon) in enumerate(NAVIGATION):
        window.navigate(name)
        assert window.pages.currentIndex() == row


def test_settings_page_covers_all_settings(window):
    assert set(window.settings_page.values()) == set(SETTINGS)


def test_settings_page_save_and_validation(window, tmp_path):
    page = window.settings_page
    page.max_jobs.setValue(3)
    page.projects_dir.setText(str(tmp_path))
    assert page.save()
    assert window.services.settings.get("max_concurrent_jobs") == 3
    assert window.services.jobs.max_concurrent == 3
    page.ffmpeg_dir.setText(str(tmp_path / "does-not-exist"))
    assert not page.save()
    assert window.services.settings.get("ffmpeg_dir") == ""


@requires_ffmpeg
def test_import_save_reopen(qtbot, window, sample_videos, tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("x")
    window.media_page.import_paths([str(sample_videos["landscape"]), str(sample_videos["vertical"]), str(bad)])
    window.media_page.import_paths([str(sample_videos["landscape"])])  # duplicate ignored
    _wait_ready(qtbot, window, 2)

    project = window.controller.project
    assert [m.status for m in project.media] == [MediaStatus.READY, MediaStatus.READY]
    table = window.media_page.table
    assert table.rowCount() == 2
    assert table.item(0, 3).text() == "320x180"
    assert table.item(1, 6).text() == "none"
    assert project.dirty and window.windowTitle().startswith("Untitled*")

    path = tmp_path / "projects" / "demo.avsproj"
    assert window._save(str(path))
    assert not project.dirty and path.is_file()

    window.controller.close_project()
    assert window.media_page.table.rowCount() == 0
    assert window.open_project(str(path))
    reopened = window.controller.project
    assert [m.metadata.resolution_text for m in reopened.media] == ["320x180", "180x320"]
    assert window.home_page.recent_list.item(0).data(Qt.ItemDataRole.UserRole) == str(path.resolve()) or \
        window.home_page.recent_list.item(0).data(Qt.ItemDataRole.UserRole) == str(path)

    jobs = window.services.jobs.store.list()
    assert sum(j.state is JobState.COMPLETED for j in jobs) == 2
    assert window.queue_page.table.rowCount() == len(jobs)


@requires_ffmpeg
def test_drop_files_on_media_table(qtbot, window, sample_videos):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(sample_videos["landscape"]))])
    table = window.media_page.table
    event = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    table.dropEvent(event)
    _wait_ready(qtbot, window, 1)
    assert table.rowCount() == 1


@requires_ffmpeg
def test_remove_and_open_in_editor(qtbot, window, sample_videos, monkeypatch):
    window.media_page.import_paths([str(sample_videos["landscape"]), str(sample_videos["vertical"])])
    _wait_ready(qtbot, window, 2)
    first_id = window.controller.project.media[0].id
    window.show()
    assert window.edit_page.preview.player.source().isEmpty()  # nothing loaded while Edit is hidden
    window.media_page._open_row_in_editor(0)
    assert window.pages.currentWidget() is window.edit_page
    assert window.edit_page._current_id == first_id
    assert not window.edit_page.preview.player.source().isEmpty()
    assert window.edit_page.info_labels["Resolution"].text() == "320x180"

    window.media_page.table.selectRow(0)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window.media_page.remove_selected()
    assert [m.name for m in window.controller.project.media] == ["vertical.mp4"]
    assert window.edit_page.media_combo.count() == 1
    assert window.edit_page.preview.player.source().fileName() == "vertical.mp4"

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Discard)
    window.close_project()
    assert window.controller.project is None
    assert window.edit_page.preview.player.source().isEmpty()  # file released


def test_missing_file_reprobe_skipped(qtbot, window, tmp_path):
    clip = tmp_path / "gone.mp4"
    clip.write_bytes(b"\0" * 64)
    window.services.jobs.pause()
    window.controller.import_paths([str(clip)])
    clip.unlink()
    item = window.controller.project.media[0]
    assert item.status is MediaStatus.MISSING
    assert window.controller.reprobe({item.id}) == 0


def test_queue_page_cancel_and_retry(qtbot, window, tmp_path):
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"\0" * 64)
    window.services.jobs.pause()
    window.controller.import_paths([str(clip)])
    page = window.queue_page
    assert page.table.rowCount() == 1
    page.table.selectRow(0)
    assert page.cancel_button.isEnabled() and not page.retry_button.isEnabled()
    page.cancel_selected()
    job = window.services.jobs.store.list()[0]
    assert job.state is JobState.CANCELLED
    page.table.selectRow(0)
    assert page.retry_button.isEnabled()
    page.retry_selected()
    assert window.services.jobs.store.get(job.id).state is JobState.QUEUED
    assert page.pause_button.text() == "Resume Queue"
    page.toggle_pause()
    qtbot.waitUntil(lambda: page.clear_button.isEnabled(), timeout=20000)
    page.clear_button.click()
    assert page.table.rowCount() == 0

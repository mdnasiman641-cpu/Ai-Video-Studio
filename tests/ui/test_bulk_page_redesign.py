"""Bulk Processing page (card layout): ticking videos, Apply to Selected, the six operation
switches reaching the real export jobs, the processing queue and summary, failures and retry
(offscreen GUI, real FFmpeg exports of tiny generated videos)."""

import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from ai_video_studio.core.errors import MediaError
from ai_video_studio.core.ffmpeg_tools import find_tool, probe_video
from ai_video_studio.jobs.models import JobState
from ai_video_studio.jobs.render_jobs import verify_output
from ai_video_studio.render.batch import RenderPreset
from ai_video_studio.render.layout import ResizeSettings
from ai_video_studio.render.output import OutputSettings
from ai_video_studio.services import Services
from ai_video_studio.ui.main_window import MainWindow
from ai_video_studio.ui.pages.bulk import COL_CHECK, COL_STATUS
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


def add_videos(qtbot, page, tmp_path, count=3, seconds=1.0):
    folder = tmp_path / "in"
    folder.mkdir(exist_ok=True)
    videos = [make_sample_video(folder / f"clip {i}.mp4", seconds=seconds) for i in range(count)]
    page.add_paths([str(v) for v in videos])
    qtbot.waitUntil(lambda: all(i.metadata is not None for i in page.batch.items), timeout=20000)
    page.output_dir.setText(str(tmp_path / "out"))
    page._on_output_changed()
    return videos


def small(**kw) -> RenderPreset:
    return RenderPreset(resize=ResizeSettings(preset="custom", width=160, height=160, mode="blur"),
                        output=OutputSettings(encoder="cpu", quality="small"), **kw)


def test_page_layout_and_switches_reach_the_export_jobs(qtbot, window, tmp_path):
    page = window.bulk_page
    assert window.pages.currentWidget() is page and page.empty_label.isVisible()
    assert not page.start_button.isEnabled()
    editor = page.editor
    cards = [editor.smart_card, editor.reframe_card, editor.caption_card, editor.resize_card, editor.speed_card,
             editor.audio_card]
    assert [c.findChild(type(page.summary), "CardTitle").text() for c in cards] == [
        "Smart Cut", "Auto Reframe", "Captions", "Resize / Crop", "Speed", "Audio Processing"]
    videos = add_videos(qtbot, page, tmp_path, count=2)
    videos[0].with_suffix(".srt").write_text("1\n00:00:00,100 --> 00:00:00,900\nHello\n", encoding="utf-8")
    assert not page.empty_label.isVisible() and page.start_button.text() == "▶  Start Batch (2)"
    qtbot.waitUntil(lambda: all(not page._thumbs.get(i.path, None) is None and not page._thumbs[i.path].isNull()
                                for i in page.batch.items), timeout=20000)  # real thumbnails

    editor.set_preset(small())
    editor.smart_card.toggle.setChecked(True)
    editor.caption_card.toggle.setChecked(True)
    editor.reframe_card.toggle.setChecked(True)  # Auto Reframe replaces Resize / Crop (one framing)
    assert not editor.resize_card.toggle.isChecked()
    editor.speed_card.toggle.setChecked(True)
    editor.effects.video_speed.setValue(1.5)
    editor.audio_card.toggle.setChecked(True)
    editor.effects.volume.setValue(80)
    page._on_shared_changed()
    shared = page.batch.shared
    assert shared.operation == "reframe" and shared.smart_cut and shared.resize.burn_captions
    assert shared.effects.speed_enabled and shared.effects.audio_enabled
    window.services.jobs.pause()
    jobs = page.start()
    assert len(jobs) == 2
    p = jobs[0].payload  # the video with a .srt next to it
    assert p["smartcut_auto"] and p["reframe_auto"]["settings"]["preset"] == "9:16"
    assert p["captions"]["segments"][0]["text"] == "Hello"
    assert p["effects"]["video_speed"] == 1.5 and p["effects"]["volume"] == 80
    assert p["output_settings"]["encoder"] == "cpu" and p["output"].startswith(str(tmp_path / "out"))
    assert "No captions found" in page.details(page.batch.items[1])  # explained, not silent
    page.cancel_items(page.batch.items)

    # everything off: a plain re-encode, with a warning
    for card in cards:
        card.toggle.setChecked(False)
    page._on_shared_changed()
    assert "No editing operation is switched on" in page.settings_error.text()
    assert page.batch.shared.operation == "none" and not page.batch.shared.smart_cut


def test_ticked_videos_apply_to_selected_and_sequential_real_exports(qtbot, window, tmp_path):
    page = window.bulk_page
    add_videos(qtbot, page, tmp_path, count=3)
    page.editor.set_preset(small())
    page._on_shared_changed()
    page.table.item(2, COL_CHECK).setCheckState(Qt.CheckState.Unchecked)  # the user unticks the third video
    assert [i.selected for i in page.batch.items] == [True, True, False]
    assert page.selected_label.text() == "2 of 3 videos selected" and page.counts["Selected"].text() == "2"
    page.editor.speed_card.toggle.setChecked(True)
    page.editor.effects.video_speed.setValue(2.0)
    page.apply_to_selected()  # the ticked videos get their own copy; nothing starts
    assert [bool(i.override) for i in page.batch.items] == [True, True, False]
    assert not any(i.job_id for i in page.batch.items)
    jobs = page.start()
    assert len(jobs) == 2 and page.batch.items[2].job_id is None  # unticked: not exported
    assert all(j.payload["effects"]["video_speed"] == 2.0 for j in jobs)
    qtbot.waitUntil(lambda: all(window.services.jobs.store.get(j.id).state.is_finished for j in jobs), timeout=90000)
    done = [window.services.jobs.store.get(j.id) for j in jobs]
    assert all(j.state is JobState.COMPLETED for j in done), [j.error for j in done]
    # one after another (Max concurrent renders = 1 by default)
    assert done[0].finished_at <= done[1].started_at
    for item in page.batch.items[:2]:
        meta = probe_video(item.output, find_tool("ffprobe"))
        assert (meta.width, meta.height) == (160, 160) and abs(meta.duration - 0.5) < 0.1  # 1 s at 2×
    page.refresh()
    assert [page.queue_table.item(r, 3).text() for r in range(2)] == ["Completed", "Completed"]
    assert page.queue_table.cellWidget(0, 5).text() == "Show File"
    assert page.counts["Completed"].text() == "2" and page.overall.value() == 100
    assert page.counts["Output size"].text() != "0 B"
    outputs = [i.output for i in page.batch.items[:2]]
    page.clear_completed()
    assert len(page.batch.items) == 1 and all(Path(o).is_file() for o in outputs)


def test_failed_video_does_not_stop_the_batch_and_can_be_retried(qtbot, window, tmp_path):
    page = window.bulk_page
    videos = add_videos(qtbot, page, tmp_path, count=2)
    page.editor.set_preset(small())
    page._on_shared_changed()
    backup = tmp_path / "backup.mp4"
    shutil.move(str(videos[0]), backup)  # the first source disappears after it was added
    jobs = page.start()
    qtbot.waitUntil(lambda: all(window.services.jobs.store.get(j.id).state.is_finished for j in jobs), timeout=90000)
    first, second = (window.services.jobs.store.get(j.id) for j in jobs)
    assert first.state is JobState.FAILED and "not found" in first.error
    assert second.state is JobState.COMPLETED  # the batch went on
    page.refresh()
    assert page.table.item(0, COL_STATUS).text() == "Failed" and "not found" in page.details(page.batch.items[0])
    assert page.queue_table.cellWidget(0, 5).text() == "Retry" and page.counts["Failed"].text() == "1"
    shutil.move(str(backup), videos[0])
    page.retry_items([page.batch.items[0]])
    qtbot.waitUntil(lambda: window.services.jobs.store.get(first.id).state is JobState.COMPLETED, timeout=60000)
    assert window.services.jobs.store.get(second.id).finished_at == second.finished_at  # not exported again


def test_unreadable_output_is_never_reported_as_completed(tmp_path):
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(MediaError, match="could not be read back"):
        verify_output(bad, find_tool("ffprobe"), 1.0)
    assert not bad.exists()
    good = make_sample_video(tmp_path / "ok.mp4", seconds=1.0)
    verify_output(good, find_tool("ffprobe"), 1.0)  # a real video passes

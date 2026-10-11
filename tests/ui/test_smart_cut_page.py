"""GUI tests for the Smart Cut page (offscreen, real FFmpeg)."""

import subprocess

import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox

from ai_video_studio.captions.model import CaptionSegment, CaptionTrack
from ai_video_studio.core.ffmpeg_tools import find_tool, probe_video
from ai_video_studio.jobs.models import JobState
from ai_video_studio.render.output import OutputSettings
from ai_video_studio.services import Services
from ai_video_studio.storage.project import Project
from ai_video_studio.ui.main_window import MainWindow
from tests.conftest import requires_ffmpeg

pytestmark = requires_ffmpeg
ON = "lt(mod(t,2),1)"


@pytest.fixture(scope="module")
def gap_clip(tmp_path_factory):
    path = tmp_path_factory.mktemp("sc") / "gaps.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=160x90:r=25:d=6",
                    "-f", "lavfi", "-i", f"aevalsrc='0.5*sin(2*PI*440*t)*{ON}':s=48000:d=6",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "6", str(path)], check=True)
    return path


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch, gap_clip):
    for name in ("warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    services = Services(tmp_path / "ui.db")
    win = MainWindow(services)
    win.show()
    win.media_page.import_paths([str(gap_clip)])
    qtbot.waitUntil(lambda: win.controller.project.media[0].metadata is not None, timeout=20000)
    win.navigate("Smart Cut")
    yield win
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Discard)
    win.close()
    win.deleteLater()
    services.close()


def analysed(qtbot, win):
    page = win.smart_cut_page
    assert page.analyze_button.isEnabled() and not page.apply_button.isEnabled()
    page.analyze()
    qtbot.waitUntil(lambda: page.table.rowCount() > 0, timeout=30000)
    qtbot.waitUntil(lambda: "Analysis finished" in page.status.text(), timeout=5000)
    return page


def test_page_is_real_not_a_placeholder(window):
    page = window.smart_cut_page
    assert window.pages.currentWidget() is page
    assert page.table.rowCount() == 0 and "no cuts applied" in page.durations.text()
    assert not page.export_preview_button.isEnabled() and page.export_button.isEnabled()


def test_analyse_review_apply_and_export(qtbot, window, tmp_path):
    page = analysed(qtbot, window)
    item = window.controller.project.media[0]
    kinds = [page.table.item(r, 1).text() for r in range(page.table.rowCount())]
    assert kinds.count("Silence") == 3  # 1-2, 3-4 and 5-6 s
    assert "proposal" in page.durations.text() and "not applied" in page.pending.text()
    # Keep the first silence; undo and redo the change.
    first_silence = kinds.index("Silence")
    page.table.selectRow(first_silence)
    page.mark_selected(False)
    assert page.table.item(first_silence, 5).text() == "Keep"
    page.undo()
    assert page.table.item(first_silence, 5).text() == "Remove"
    page.redo()
    assert page.table.item(first_silence, 5).text() == "Keep"
    # Changing a setting re-detects (undoably) and the timeline follows.
    page.min_silence.setValue(2.0)
    assert page.table.rowCount() == 1  # every silence is shorter than 2 s: nothing proposed
    page.undo()
    assert page.table.rowCount() == 6
    # Move a boundary by editing the table.
    row = kinds.index("Silence", first_silence + 1)
    page.table.item(row, 2).setText("00:03.400")
    seg = item.smart_cut.segments[row]
    assert seg.start == 3.4 and item.smart_cut.segments[row - 1].end == 3.4
    page.apply_cuts()
    assert item.smart_cut.cut_ranges() and not page.pending.text().startswith("The reviewed")
    assert "with cuts applied" in page.durations.text()
    output = tmp_path / "cut.mp4"
    job = page.start_export(str(output), OutputSettings(encoder="cpu", quality="small"))
    assert job is not None
    qtbot.waitUntil(lambda: window.services.jobs.store.get(job.id).state.is_finished, timeout=60000)
    done = window.services.jobs.store.get(job.id)
    assert done.state is JobState.COMPLETED, done.error
    expected = sum(b - a for a, b in item.smart_cut.cut_ranges())
    assert abs(probe_video(str(output), find_tool("ffprobe")).duration - expected) < 0.1
    qtbot.waitUntil(lambda: page.open_folder_button.isEnabled(), timeout=5000)
    assert "Exported" in page.export_status.text()
    # The same output cannot be queued twice, and the source is never an output.
    assert page.start_export(str(window.controller.project.media[0].path), OutputSettings()) is None


def test_captions_follow_cuts_in_srt_and_project_keeps_state(qtbot, window, tmp_path, monkeypatch):
    page = analysed(qtbot, window)
    item = window.controller.project.media[0]
    window.controller.set_captions(item.id, CaptionTrack([CaptionSegment(1.3, 1.8, "inside a silence"),
                                                          CaptionSegment(4.2, 4.8, "after cuts")]))
    page.apply_cuts()
    assert "1 caption(s) lie entirely in removed parts" in page.pending.text()
    srt = tmp_path / "edited.srt"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(srt), ""))
    page.export_srt()
    text = srt.read_text(encoding="utf-8")
    assert "inside a silence" not in text and "after cuts" in text
    assert item.captions.segments[1].start == 4.2  # the caption track keeps its own timing
    path = window.controller.save_project(tmp_path / "p.avsproj")
    again = Project.load(path).media[0].smart_cut
    assert again.applied == item.smart_cut.applied and len(again.segments) == len(item.smart_cut.segments)
    page.reset_analysis()
    assert item.smart_cut is None and page.table.rowCount() == 0

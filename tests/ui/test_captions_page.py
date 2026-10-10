"""GUI tests for the Captions page (offscreen). Whisper and AI providers are faked."""

import json
import subprocess

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QMessageBox, QTableWidgetSelectionRange

from ai_video_studio.captions import transcriber
from ai_video_studio.captions.style import BUILTIN_PRESETS, CaptionStyle
from ai_video_studio.jobs.caption_jobs import CORRECTION_JOB, TRANSCRIBE_JOB, make_correction_handler, make_transcribe_handler
from ai_video_studio.jobs.models import JobState
from ai_video_studio.services import Services
from ai_video_studio.storage.project import Project
from ai_video_studio.ui.caption_dialogs import CorrectionReviewDialog, diff_html
from ai_video_studio.ui.main_window import MainWindow
from ai_video_studio.ui.pages.captions import COL_END, COL_START, COL_TEXT
from ai_video_studio.ui.widgets.caption_overlay import paint_captions
from tests.conftest import requires_ffmpeg
from tests.fakes import FakeFactory, FakeWhisperModel, fake_segments

pytestmark = requires_ffmpeg

WORDS = [[(0.2, 0.6, " Hello"), (0.7, 1.1, " everyone.")], [(1.4, 1.8, " Welcome"), (1.9, 2.4, " back.")]]


@pytest.fixture(scope="module")
def speech_clip(tmp_path_factory):
    path = tmp_path_factory.mktemp("cp") / "talk.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=navy:s=640x360:r=25:d=3",
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch, speech_clip):
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    transcriber.clear_model_cache()
    services = Services(tmp_path / "ui.db")
    services.jobs.register(TRANSCRIBE_JOB, make_transcribe_handler(
        services.ffmpeg_path, model_factory=FakeFactory(FakeWhisperModel(fake_segments(WORDS))), cuda_devices=0))
    win = MainWindow(services)
    win.show()
    win.media_page.import_paths([str(speech_clip)])
    qtbot.waitUntil(lambda: win.controller.project.media[0].metadata is not None, timeout=20000)
    win.navigate("Captions")
    yield win
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Discard)
    win.close()
    win.deleteLater()
    services.close()


def _generate(qtbot, win):
    page = win.captions_page
    media_id = win.controller.project.media[0].id
    win.controller.submit_transcription([media_id], "en", "tiny", "cpu")
    qtbot.waitUntil(lambda: page.table.rowCount() == 2, timeout=30000)
    return page, media_id


def texts(page):
    return [page.table.item(r, COL_TEXT).text() for r in range(page.table.rowCount())]


def test_generate_and_edit_with_undo(qtbot, window):
    page, media_id = _generate(qtbot, window)
    assert texts(page) == ["Hello everyone.", "Welcome back."]
    assert "CPU" in page.status.text()
    assert window.controller.project.dirty

    page.table.item(0, COL_TEXT).setText("Hello, everyone!")
    assert window.controller.project.media[0].captions.segments[0].text == "Hello, everyone!"

    page.table.item(1, COL_END).setText("00:00.100")        # end before start -> rejected and reverted
    assert "⚠" in page.status.text()
    assert page.table.item(1, COL_END).text() == "00:02.400"
    page.table.item(1, COL_START).setText("00:01.500")
    assert window.controller.project.media[0].captions.segments[1].start == 1.5

    page.undo()
    assert window.controller.project.media[0].captions.segments[1].start == 1.4
    page.redo()
    assert window.controller.project.media[0].captions.segments[1].start == 1.5

    page.table.setRangeSelected(QTableWidgetSelectionRange(0, 0, 1, 3), True)
    page.merge_selected()
    assert texts(page) == ["Hello, everyone! Welcome back."]

    wait_player_ready(qtbot, page)
    page.preview.seek(1000)
    qtbot.waitUntil(lambda: abs(page.preview.position_seconds() - 1.0) < 0.2, timeout=5000)
    at = page.preview.position_seconds()
    page.table.clearSelection()
    page.split_at_playhead()
    assert len(texts(page)) == 2
    assert window.controller.project.media[0].captions.segments[0].end == pytest.approx(at, abs=0.001)

    page.table.selectRow(1)
    page.delete_selected()
    assert len(texts(page)) == 1
    for _ in range(5):
        page.undo()
    assert texts(page) == ["Hello everyone.", "Welcome back."]


def wait_player_ready(qtbot, page):
    qtbot.waitUntil(lambda: page.preview.status_label.text() == "Ready", timeout=10000)


def test_add_caption_without_existing_track(qtbot, window):
    page = window.captions_page
    wait_player_ready(qtbot, page)
    page.preview.seek(500)
    qtbot.waitUntil(lambda: page.preview.position_seconds() > 0.3, timeout=5000)
    page.add_at_playhead()
    track = window.controller.project.media[0].captions
    assert len(track.segments) == 1 and track.segments[0].text == "New caption"
    page.undo()
    assert window.controller.project.media[0].captions is None


def test_srt_import_export_and_persistence(qtbot, window, tmp_path):
    page = window.captions_page
    srt = tmp_path / "in.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:01,500\nবাংলা লাইন\n\nbad block\n\n"
                   "2\n00:00:02,000 --> 00:00:02,800\nSecond\n", encoding="utf-8")
    assert page.import_srt_file(str(srt))
    assert texts(page) == ["বাংলা লাইন", "Second"]
    assert "skipped" in page.status.text()

    item = window.controller.project.media[0]
    page.style_editor.size.setValue(77)
    page.style_editor.vertical.setCurrentText("top")
    assert item.caption_style.font_size == 77 and item.caption_style.vertical == "top"

    from ai_video_studio.captions.srt import write_srt_file, read_srt_file
    out = write_srt_file(tmp_path / "out.srt", item.captions)
    assert [s.text for s in read_srt_file(out).segments] == ["বাংলা লাইন", "Second"]

    path = tmp_path / "proj.avsproj"
    assert window._save(str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 2 and data["media"][0]["caption_style"]["font_size"] == 77
    loaded = Project.load(path).media[0]
    assert [s.text for s in loaded.captions.segments] == ["বাংলা লাইন", "Second"]
    assert loaded.caption_style.vertical == "top"


def test_presets_apply_to_selected_videos(qtbot, window, sample_videos):
    window.media_page.import_paths([str(sample_videos["landscape"]), str(sample_videos["vertical"])])
    qtbot.waitUntil(lambda: all(m.metadata for m in window.controller.project.media), timeout=20000)
    page = window.captions_page
    services = window.services
    services.presets.save("My Yellow", CaptionStyle(text_color="#FFFF00", font_size=55))
    page.reload_presets(select="My Yellow")
    assert page.delete_preset_button.isEnabled()
    media = window.controller.project.media
    page.apply_style_to([media[1].id, media[2].id], services.presets.get("My Yellow"))
    assert media[0].caption_style is None
    assert media[1].caption_style.text_color == "#FFFF00" and media[2].caption_style.font_size == 55
    page.reload_presets(select="Boxed")
    assert not page.delete_preset_button.isEnabled()
    page.apply_preset()
    assert media[0].caption_style == BUILTIN_PRESETS["Boxed"]


def test_overlay_paints_caption_region():
    image = QImage(640, 360, QImage.Format.Format_RGB32)
    image.fill(QColor("black"))
    painter = QPainter(image)
    paint_captions(painter, QRectF(0, 0, 640, 360), ["Overlay test"],
                   CaptionStyle(text_color="#FFFFFF", vertical="bottom"))
    painter.end()

    def lit(y0, y1):
        return sum(1 for y in range(y0, y1) for x in range(0, 640, 2) if QColor(image.pixel(x, y)).lightness() > 128)

    assert lit(250, 345) > 50 and lit(0, 150) == 0

    boxed = QImage(640, 360, QImage.Format.Format_RGB32)
    boxed.fill(QColor("white"))
    painter = QPainter(boxed)
    paint_captions(painter, QRectF(0, 0, 640, 360), ["Top"], CaptionStyle(vertical="top", background=True,
                                                                         background_opacity=100))
    painter.end()
    assert QColor(boxed.pixel(320, 50)).lightness() < 100 or QColor(boxed.pixel(320, 40)).lightness() < 100


@requires_ffmpeg
def test_export_video_with_captions(qtbot, window, tmp_path):
    page, media_id = _generate(qtbot, window)
    window.services.settings.set("video_encoder", "libx264")
    out = tmp_path / "exported.mp4"
    job = page.start_export(str(out))
    qtbot.waitUntil(lambda: window.services.jobs.store.get(job.id).state.is_finished, timeout=60000)
    job = window.services.jobs.store.get(job.id)
    assert job.state is JobState.COMPLETED, job.error
    assert out.is_file()
    qtbot.waitUntil(lambda: "Exported" in page.status.text(), timeout=5000)
    # refuses to overwrite the source
    assert page.start_export(window.controller.project.media[0].path) is None


def test_ai_correction_review_flow(qtbot, window, monkeypatch):
    page, media_id = _generate(qtbot, window)
    track = window.controller.project.media[0].captions
    first_id, second_id = track.segments[0].id, track.segments[1].id

    def transport(url, headers, body, timeout):
        sent = json.loads(body["contents"][0]["parts"][0]["text"])
        assert [c["id"] for c in sent["captions"]] == [first_id, second_id]
        answer = {"corrections": [{"id": first_id, "text": "Hello, everyone."},
                                  {"id": second_id, "text": "Welcome back!"}]}
        return 200, {"candidates": [{"content": {"parts": [{"text": json.dumps(answer)}]}}]}

    window.services.jobs.register(CORRECTION_JOB, make_correction_handler(lambda p: "test-key", transport=transport))
    monkeypatch.setattr(window.services, "ai_correction_config", lambda: ("gemini", "gemini-test"))
    page._update_buttons()
    assert page.ai_button.isEnabled()
    window.controller.submit_correction(media_id)
    qtbot.waitUntil(lambda: page.review_button.isEnabled(), timeout=20000)
    assert page.review_button.text() == "Review Suggestions (2)"
    assert texts(page) == ["Hello everyone.", "Welcome back."]       # nothing applied automatically

    dialog = CorrectionReviewDialog(track.pending_corrections)
    assert dialog.accepted_ids() == set()                            # unchecked by default
    dialog.set_checked(0, True)
    page.finish_review(CorrectionReviewDialog.APPLY, dialog.accepted_ids())
    assert texts(page) == ["Hello, everyone.", "Welcome back."]
    assert not window.controller.project.media[0].captions.pending_corrections
    assert not page.review_button.isEnabled()
    page.undo()
    assert texts(page)[0] == "Hello everyone."


def test_ai_button_disabled_without_configuration(qtbot, window):
    page, _ = _generate(qtbot, window)
    assert not page.ai_button.isEnabled() and "Settings" in page.ai_button.toolTip()


def test_diff_html_marks_changes():
    left, right = diff_html("helo world", "hello world")
    assert "line-through" in left and "helo" in left and "hello" in right


def test_transcription_result_recovered_after_restart(qtbot, window, tmp_path):
    controller = window.controller
    path = tmp_path / "rec.avsproj"
    window._save(str(path))
    media_id = controller.project.media[0].id
    window.services.jobs.pause()
    controller.submit_transcription([media_id], "en", "tiny", "cpu")
    window.close_project()
    window.services.jobs.resume()
    qtbot.waitUntil(lambda: window.services.jobs.store.list({JobState.COMPLETED})
                    and any(j.kind == TRANSCRIBE_JOB for j in window.services.jobs.store.list({JobState.COMPLETED})),
                    timeout=30000)
    assert window.open_project(str(path))
    assert [s.text for s in window.controller.project.media[0].captions.segments] == ["Hello everyone.",
                                                                                      "Welcome back."]


def test_generate_dialog_bengali_model_notes(window):
    from ai_video_studio.ui.caption_dialogs import (
        BENGALI_LARGE_NOTE,
        BENGALI_UNUSABLE_NOTE,
        GenerateCaptionsDialog,
    )

    dialog = GenerateCaptionsDialog(window.controller.project.media, window.controller.project.media[0].id,
                                    window.services.settings)
    dialog.language.setCurrentIndex(dialog.language.findData("bn"))
    for model in ("tiny", "base", "small", "medium"):
        dialog.model.setCurrentText(model)
        assert dialog.model_note.text() == BENGALI_UNUSABLE_NOTE, model
    for model in ("large-v3", "large-v3-turbo"):
        dialog.model.setCurrentText(model)
        assert dialog.model_note.text() == BENGALI_LARGE_NOTE, model
    dialog.language.setCurrentIndex(dialog.language.findData("en"))
    assert dialog.model_note.text() == ""
    assert dialog.values()["language"] == "en"

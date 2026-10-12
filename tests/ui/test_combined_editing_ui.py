"""GUI tests: final export on the Edit page, caption templates on the Captions page, and the new
Bulk Processing actions (offscreen, real FFmpeg)."""

import pytest
from PySide6.QtWidgets import QMessageBox

from ai_video_studio.captions.model import CaptionSegment, CaptionTrack
from ai_video_studio.captions.style import BUILTIN_PRESETS, CaptionStyle
from ai_video_studio.core.ffmpeg_tools import find_tool, probe_video
from ai_video_studio.jobs.models import JobState
from ai_video_studio.jobs.render_jobs import RENDER_FRAME_JOB, RENDER_JOB
from ai_video_studio.render.batch import RenderPreset
from ai_video_studio.render.effects import EditEffects
from ai_video_studio.render.layout import ResizeSettings
from ai_video_studio.render.output import OutputSettings
from ai_video_studio.services import Services
from ai_video_studio.smartcut.model import SmartCutState
from ai_video_studio.ui.main_window import MainWindow
from ai_video_studio.ui.pages import bulk as bulk_module
from tests.conftest import make_sample_video, requires_ffmpeg

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
    yield win
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Discard)
    win.close()
    win.deleteLater()
    services.close()


def prepare(window):
    """Captions, applied Smart Cut cuts and Resize & Crop settings on the test video (3 s)."""
    c = window.controller
    item = c.project.media[0]
    c.set_captions(item.id, CaptionTrack([CaptionSegment(0.2, 0.8, "first"), CaptionSegment(2.2, 2.8, "last")]))
    item.smart_cut = SmartCutState(applied=[(0.0, 1.0), (2.0, 3.0)])
    c.smartcut_changed.emit(item.id)  # as when the cuts are applied on the Smart Cut page
    c.set_resize_settings(item.id, ResizeSettings(preset="custom", width=320, height=320, mode="blur",
                                                  burn_captions=True))
    return item


def test_edit_page_exports_every_chosen_operation_once(qtbot, window, tmp_path):
    item = prepare(window)
    window.navigate("Edit")
    page = window.edit_page
    form = page.effects_form
    form.speed_enabled.setChecked(True)
    form.video_speed.setValue(2.0)
    form.audio_enabled.setChecked(True)
    form.volume.setValue(150)
    stored = window.controller.effects(item.id)
    assert stored.video_factor == 2.0 and stored.volume == 150  # saved with the project, source untouched
    assert page.preview.player.playbackRate() == 2.0
    assert "speed 2×" in page.effects_summary.text() and "volume 150%" in page.effects_summary.text()
    assert page.use_cuts.isChecked() and page.use_resize.isChecked() and page.burn_captions.isChecked()

    output = tmp_path / "final.mp4"
    job = page.start_export(str(output), OutputSettings(encoder="cpu", quality="small"))
    p = job.payload
    assert job.kind == RENDER_JOB and p["keep_ranges"] == [[0.0, 1.0], [2.0, 3.0]]
    assert p["stage"]["type"] == "layout" and p["effects"]["video_speed"] == 2.0
    # captions are on the final timeline: cut (2.2 s → 1.2 s), then twice as fast (→ 0.6 s)
    assert "Dialogue: 1,0:00:00.10,0:00:00.40,Default" in p["ass"]
    assert "Dialogue: 1,0:00:00.60,0:00:00.90,Default" in p["ass"]
    qtbot.waitUntil(lambda: window.services.jobs.store.get(job.id).state.is_finished, timeout=60000)
    done = window.services.jobs.store.get(job.id)
    assert done.state is JobState.COMPLETED, done.error
    meta = probe_video(str(output), find_tool("ffprobe"))
    assert (meta.width, meta.height) == (320, 320) and abs(meta.duration - 1.0) < 0.1 and meta.has_audio

    # switching operations off leaves them out of the export
    page.use_cuts.setChecked(False)
    page.use_resize.setChecked(False)
    page.burn_captions.setChecked(False)
    form.speed_enabled.setChecked(False)
    form.audio_enabled.setChecked(False)
    job = page.start_export(str(tmp_path / "plain.mp4"), OutputSettings(encoder="cpu", quality="small"))
    p = job.payload
    assert p["keep_ranges"] is None and p["stage"] is None and p["ass"] is None and p["effects"] is None
    assert window.controller.project.media[0].path != str(output)


def test_caption_templates_apply_to_selected_all_and_reset(window):
    item = prepare(window)
    window.navigate("Captions")
    page = window.captions_page
    assert "Bold Green" in page.template_grid.buttons and "Highlight Style" in page.template_grid.buttons
    page.template_grid.buttons["Bold Green"].click()
    assert page.preset_combo.currentData() == "Bold Green"
    page.table.selectRow(1)
    page.apply_preset_to_selected()
    segments = window.controller.project.media[0].captions.segments
    assert segments[0].style is None and segments[1].style == BUILTIN_PRESETS["Bold Green"].to_dict()
    assert "1 caption(s) have their own style" in page.override_info.text()
    ass = window.controller.caption_ass(item.id)
    assert "Style: S1," in ass and ",S1,,0,0,0,,last" in ass  # the export uses the caption's own style
    page.undo()
    assert window.controller.project.media[0].captions.segments[1].style is None
    page.redo()
    page.template_grid.buttons["Large Readable"].click()
    page.apply_preset()  # all captions
    item = window.controller.project.media[0]
    assert all(s.style is None for s in item.captions.segments)
    assert item.caption_style == BUILTIN_PRESETS["Large Readable"]
    assert [s.text for s in item.captions.segments] == ["first", "last"]  # text and times untouched
    page.reset_style()
    assert window.controller.project.media[0].effective_caption_style == CaptionStyle()


def test_bulk_apply_to_all_and_preview(qtbot, window, tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(bulk_module.FramePreviewDialog, "exec", lambda self: shown.append(self.windowTitle()) or 0)
    window.navigate("Bulk Processing")
    page = window.bulk_page
    folder = tmp_path / "in"
    folder.mkdir()
    videos = [make_sample_video(folder / f"v{i}.mp4", seconds=1.0) for i in range(3)]
    page.add_paths([str(v) for v in videos])
    qtbot.waitUntil(lambda: all(i.metadata is not None for i in page.batch.items), timeout=20000)
    shared = RenderPreset(resize=ResizeSettings(preset="custom", width=160, height=160, mode="blur",
                                                burn_captions=True),
                          effects=EditEffects(speed_enabled=True, video_speed=1.5), smart_cut=True,
                          output=OutputSettings(encoder="cpu", quality="small"))
    page.editor.set_preset(shared)
    page._on_shared_changed()
    assert page.batch.shared.smart_cut and page.batch.shared.effects.video_factor == 1.5
    page.batch.items[0].override = RenderPreset(operation="none")
    page.batch.items[2].override = RenderPreset(operation="none")
    page.refresh()
    assert page.all_shared_button.isEnabled()
    page.apply_shared_to_all()
    assert all(i.override is None for i in page.batch.items)
    page.table.selectRow(1)
    job = page.preview_selected()
    assert job.kind == RENDER_FRAME_JOB and job.payload["stage"]["layout"]["out_w"] == 160
    qtbot.waitUntil(lambda: bool(shown), timeout=30000)
    assert shown == [f"Batch Preview at {job.payload['at']:.2f} s"]

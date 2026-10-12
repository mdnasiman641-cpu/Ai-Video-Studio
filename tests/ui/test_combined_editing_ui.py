"""GUI tests: final export on the Edit page, caption templates on the Captions page, and the new
Bulk Processing actions (offscreen, real FFmpeg)."""

from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox

from ai_video_studio.captions.model import CaptionSegment, CaptionTrack
from ai_video_studio.captions.style import BUILTIN_PRESETS, CaptionStyle
from ai_video_studio.core.ffmpeg_tools import find_tool, probe_video
from ai_video_studio.jobs.models import JobState
from ai_video_studio.jobs.render_jobs import RENDER_FRAME_JOB
from ai_video_studio.render.batch import RenderPreset
from ai_video_studio.render.effects import EditEffects
from ai_video_studio.render.layout import ResizeSettings
from ai_video_studio.render.output import OutputSettings
from ai_video_studio.services import Services
from ai_video_studio.smartcut.levels import AudioLevels
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


def test_edit_toolbar_opens_every_tool_inside_the_page(window):
    window.navigate("Edit")
    page = window.edit_page
    assert list(page.tool_buttons) == ["smart_cut", "captions", "resize", "reframe", "speed", "audio", "final"]
    assert [b.text() for b in page.tool_buttons.values()] == [
        "Smart Cut", "Captions", "Resize / Crop", "Auto Reframe", "Speed", "Audio", "Final Export"]
    for key, button in page.tool_buttons.items():
        button.click()
        assert page.current_tool() == key and button.isChecked()
        assert window.pages.currentWidget() is page  # never leaves the Edit page
    assert page.panels["speed"].isAncestorOf(page.effects_form.speed_group)
    assert page.panels["audio"].isAncestorOf(page.effects_form.audio_group)
    assert page.export_button.isEnabled()


def test_edit_page_settings_are_saved_and_reach_the_final_export(qtbot, window, tmp_path):
    item = prepare(window)
    window.navigate("Edit")
    page = window.edit_page
    c = window.controller
    page.show_tool("speed")
    form = page.effects_form
    form.speed_enabled.setChecked(True)
    form.video_speed.setValue(2.0)
    page.show_tool("audio")
    form.audio_enabled.setChecked(True)
    form.volume.setValue(150)
    assert c.effects(item.id).video_factor == 2.0 and c.effects(item.id).volume == 150  # saved in the project
    assert page.preview.player.playbackRate() == 2.0
    page.show_tool("resize")
    resize = page.panels["resize"]
    resize.width_spin.setValue(360)
    assert item.resize.width == 360 and item.resize.enabled  # the same settings the Resize & Crop page uses
    resize.width_spin.setValue(320)
    smart = page.panels["smart_cut"]
    assert smart.include.isEnabled() and smart.include.isChecked()
    smart.include.setChecked(False)
    assert item.resize.use_cuts is False and not c.final_operations(item.id)["use_cuts"]
    smart.include.setChecked(True)
    page.show_tool("final")
    summary = page.panels["final"].summary.text()
    for part in ("✔ <b>Smart Cut</b>", "✔ <b>Captions</b>", "✔ <b>Framing</b>: Resize / Crop, 320×320",
                 "✔ <b>Speed</b>: speed 2×", "✔ <b>Audio</b>: volume 150%"):
        assert part in summary, summary

    output = tmp_path / "final.mp4"
    job = page.start_export(str(output), OutputSettings(encoder="cpu", quality="small"))
    p = job.payload
    assert p["keep_ranges"] == [[0.0, 1.0], [2.0, 3.0]] and p["stage"]["type"] == "layout"
    assert p["effects"]["video_speed"] == 2.0 and p["effects"]["volume"] == 150
    # captions on the final timeline: cut (2.2 s → 1.2 s), then twice as fast (→ 0.6 s)
    assert "Dialogue: 1,0:00:00.10,0:00:00.40,Default" in p["ass"]
    assert "Dialogue: 1,0:00:00.60,0:00:00.90,Default" in p["ass"]
    qtbot.waitUntil(lambda: window.services.jobs.store.get(job.id).state.is_finished, timeout=60000)
    done = window.services.jobs.store.get(job.id)
    assert done.state is JobState.COMPLETED, done.error
    meta = probe_video(str(output), find_tool("ffprobe"))
    assert (meta.width, meta.height) == (320, 320) and abs(meta.duration - 1.0) < 0.1 and meta.has_audio
    assert item.path != str(output) and Path(item.path).is_file()

    # every switch off: nothing is applied
    smart.include.setChecked(False)
    page.panels["captions"].burn.setChecked(False)
    resize.enabled.setChecked(False)
    form.speed_enabled.setChecked(False)
    form.audio_enabled.setChecked(False)
    assert "— <b>Framing</b>: original frame" in page.summary_html(item)
    p = page.start_export(str(tmp_path / "plain.mp4"), OutputSettings(encoder="cpu", quality="small")).payload
    assert p["keep_ranges"] is None and p["stage"] is None and p["ass"] is None and p["effects"] is None


def test_smart_cut_tool_detects_and_applies_cuts(window):
    window.navigate("Edit")
    page = window.edit_page
    c = window.controller
    item = c.project.media[0]
    levels = AudioLevels([-20] * 40 + [-80] * 40 + [-20] * 40, [], 3.0)  # silence from 1 s to 2 s
    c.set_smartcut_state(item.id, SmartCutState(levels=levels))
    page.show_tool("smart_cut")
    smart = page.panels["smart_cut"]
    assert not smart.include.isEnabled()  # nothing applied yet
    smart.min_silence.setValue(0.5)  # a settings change detects the silence again (existing algorithm)
    assert smart.cuts.count() == 1 and "proposed for removal" in smart.status.text()
    smart.apply_button.click()
    cuts = c.applied_cuts(item.id)
    assert cuts and len(cuts) == 2 and smart.include.isChecked() and c.final_operations(item.id)["use_cuts"]
    smart.remove_button.click()
    assert c.applied_cuts(item.id) is None and not smart.include.isEnabled()


def test_caption_and_reframe_tools(window, monkeypatch, tmp_path):
    item = prepare(window)
    window.navigate("Edit")
    page = window.edit_page
    c = window.controller
    page.show_tool("captions")
    caps = page.panels["captions"]
    assert caps.table.rowCount() == 2 and caps.burn.isChecked()
    caps.table.item(1, 2).setText("edited text")
    assert item.captions.segments[1].text == "edited text"
    caps.table.item(1, 1).setText("not a time")
    assert "⚠" in caps.status.text() and item.captions.segments[1].end == 2.8
    caps.templates.buttons["Red Outline"].click()
    caps.table.selectRow(0)
    caps.apply_to_selected()
    assert item.captions.segments[0].style == BUILTIN_PRESETS["Red Outline"].to_dict()
    assert item.captions.segments[1].style is None
    caps.apply_to_all()
    assert all(s.style is None for s in item.captions.segments)
    assert item.caption_style == BUILTIN_PRESETS["Red Outline"]
    caps.style_editor.size.setValue(70)
    assert item.caption_style.font_size == 70
    called = []
    monkeypatch.setattr(window.captions_page, "import_srt", lambda: called.append(window.captions_page.current().id))
    caps.import_button.click()  # the Captions page's own dialog, for this video
    assert called == [item.id] and window.pages.currentWidget() is page

    page.show_tool("reframe")
    reframe = page.panels["reframe"]
    reframe.use.setChecked(True)
    assert item.resize.mode == "reframe" and c.final_operations(item.id)["reframe"]
    assert "⚠" in reframe.status.text() and "analysis" in reframe.status.text()
    assert page.start_export(str(tmp_path / "r.mp4"), OutputSettings(encoder="cpu")) is None  # clear error
    reframe.tracking.setCurrentIndex(reframe.tracking.findData("center"))
    assert c.reframe_state(item.id).settings.tracking_mode == "center"
    assert "Ready" in reframe.status.text()  # centre crop needs no analysis
    reframe.use.setChecked(False)
    assert item.resize.mode == "blur"  # the previous Resize / Crop mode comes back


def test_frame_preview_and_settings_to_bulk(qtbot, window, monkeypatch):
    from ai_video_studio.ui import caption_dialogs

    shown = []
    monkeypatch.setattr(caption_dialogs.FramePreviewDialog, "exec", lambda self: shown.append(self.windowTitle()) or 0)
    prepare(window)
    window.navigate("Edit")
    page = window.edit_page
    page.effects_form.speed_enabled.setChecked(True)
    page.effects_form.video_speed.setValue(1.5)
    job = page.preview_frame()
    assert job.payload["stage"]["layout"]["out_w"] == 320 and "Style: Default" in job.payload["ass"]
    qtbot.waitUntil(lambda: bool(shown), timeout=30000)
    assert shown[0].startswith("Final Frame Preview")
    page.panels["final"].bulk_button.click()
    assert window.pages.currentWidget() is window.bulk_page
    shared = window.bulk_page.batch.shared
    assert shared.operation == "resize" and shared.resize.width == 320 and shared.resize.burn_captions
    assert shared.smart_cut and shared.effects.video_factor == 1.5


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

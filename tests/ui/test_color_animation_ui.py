"""Bulk Processing colour grading / video filters / caption animation cards and the Edit page's
caption Animation tab (offscreen GUI, real FFmpeg renders of tiny generated videos)."""

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QInputDialog, QMessageBox

from ai_video_studio.captions.model import CaptionSegment, CaptionTrack
from ai_video_studio.core.ffmpeg_tools import find_tool, probe_video
from ai_video_studio.jobs.models import JobState
from ai_video_studio.render.batch import RenderPreset
from ai_video_studio.render.layout import ResizeSettings
from ai_video_studio.render.output import OutputSettings
from ai_video_studio.services import Services
from ai_video_studio.ui import caption_dialogs
from ai_video_studio.ui.main_window import MainWindow
from ai_video_studio.ui.pages import bulk as bulk_module
from ai_video_studio.ui.pages.bulk import COL_CHECK
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
    yield win
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Discard)
    win.close()
    win.deleteLater()
    services.close()


def add_videos(qtbot, page, tmp_path, count=2):
    folder = tmp_path / "in"
    folder.mkdir(exist_ok=True)
    videos = [make_sample_video(folder / f"clip {i}.mp4", seconds=1.5) for i in range(count)]
    for v in videos:
        v.with_suffix(".srt").write_text("1\n00:00:00,100 --> 00:00:01,400\nআমি বাংলায় Hello\n", encoding="utf-8")
    page.add_paths([str(v) for v in videos])
    qtbot.waitUntil(lambda: all(i.metadata is not None for i in page.batch.items), timeout=20000)
    page.output_dir.setText(str(tmp_path / "out"))
    page._on_output_changed()
    return videos


def test_bulk_combines_captions_animation_color_filter_framing_and_smart_cut(qtbot, window, tmp_path):
    window.navigate("Bulk Processing")
    page = window.bulk_page
    add_videos(qtbot, page, tmp_path)
    e = page.editor
    e.set_preset(RenderPreset(resize=ResizeSettings(preset="custom", width=180, height=320, mode="blur"),
                              output=OutputSettings(encoder="cpu", quality="small")))
    assert not e.color_card.body.isVisible() and not e.anim_card.body.isVisible()  # switched-off cards collapse
    e.smart_card.toggle.setChecked(True)
    e.caption_card.toggle.setChecked(True)
    e.anim_card.toggle.setChecked(True)
    assert e.anim_card.body.isVisible()
    e.animation.entrance.setCurrentIndex(e.animation.entrance.findData("pop"))
    e.color_card.toggle.setChecked(True)
    e.grade.look.setCurrentIndex(e.grade.look.findData("builtin:cinematic"))
    e.grade.look.activated.emit(e.grade.look.currentIndex())
    assert e.grade.sliders["contrast"].value() == 18 and "Cinematic" in e.grade.active.text()
    e.filter_card.toggle.setChecked(True)
    e.filters.buttons["vibrant"].click()
    e.filters.intensity.setValue(35)
    assert e.filters.current.text() == "Filter: Vibrant at 35%"
    page._on_shared_changed()
    assert page.settings_error.text() == ""

    # only the ticked video gets these settings; nothing starts yet
    page.table.item(1, COL_CHECK).setCheckState(Qt.CheckState.Unchecked)
    page.apply_to_selected()
    first, second = page.batch.items
    assert first.override is not None and second.override is None and first.job_id is None
    assert "Animation: In: Pop In" in page.table.item(0, bulk_module.COL_SETTINGS).toolTip()

    jobs = page.start()
    assert len(jobs) == 1 and second.job_id is None
    p = jobs[0].payload
    assert p["color"]["values"]["contrast"] == 18 and p["color"]["filter"] == "vibrant" and p["color"]["intensity"] == 35
    assert p["captions"]["animation"]["entrance"] == "pop" and p["captions"]["segments"][0]["text"].startswith("আমি")
    assert p["smartcut_auto"] and p["stage"]["type"] == "layout"
    qtbot.waitUntil(lambda: window.services.jobs.store.get(jobs[0].id).state.is_finished, timeout=90000)
    done = window.services.jobs.store.get(jobs[0].id)
    assert done.state is JobState.COMPLETED, done.error
    assert any(n.startswith("Colour: colour grade Cinematic, filter Vibrant 35%") for n in done.result["warnings"])
    meta = probe_video(first.output, find_tool("ffprobe"))
    assert (meta.width, meta.height) == (180, 320)


def test_before_after_renders_two_real_frames_and_caches_them(qtbot, window, tmp_path, monkeypatch):
    window.navigate("Bulk Processing")
    page = window.bulk_page
    add_videos(qtbot, page, tmp_path, count=1)
    shown = []
    monkeypatch.setattr(bulk_module.BeforeAfterDialog, "exec", lambda self: shown.append(self) or 0)
    assert page.compare_colors() == [] and "Switch on Color Grading" in page.summary.text()
    page.editor.filter_card.toggle.setChecked(True)
    page.editor.filters.buttons["black_white"].click()
    page._on_shared_changed()
    jobs = page.compare_colors()
    assert len(jobs) == 2 and jobs[0].payload["color"] is None and jobs[1].payload["color"]["filter"] == "black_white"
    qtbot.waitUntil(lambda: len(shown) == 1, timeout=30000)
    images = [window.services.jobs.store.get(j.id).result["image"] for j in jobs]
    assert Path(images[0]).read_bytes() != Path(images[1]).read_bytes()
    assert page.compare_colors() == [] and len(shown) == 2  # cached: shown again without rendering


def test_custom_color_presets_save_load_update_delete(qtbot, window, monkeypatch):
    window.navigate("Bulk Processing")
    grade = window.bulk_page.editor.grade
    store = window.services.color_presets
    grade.sliders["temperature"].setValue(40)
    assert "Custom" in grade.active.text()
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Warm Doc", True))
    assert grade.save_preset() == "Warm Doc"
    assert store.get("Warm Doc") == {"temperature": 40.0} and grade.look.currentText() == "★ Warm Doc"
    grade.reset()
    assert grade.values() == {} and "Original" in grade.active.text()
    grade.look.setCurrentIndex(grade.look.findData("custom:Warm Doc"))
    grade.look.activated.emit(grade.look.currentIndex())
    assert grade.sliders["temperature"].value() == 40
    grade.sliders["contrast"].setValue(10)  # a change makes it Custom; the stored preset is unchanged
    assert store.get("Warm Doc") == {"temperature": 40.0}
    grade.look.setCurrentIndex(grade.look.findData("custom:Warm Doc"))
    grade.look.activated.emit(grade.look.currentIndex())
    grade.sliders["temperature"].blockSignals(True)
    grade.sliders["temperature"].setValue(25)
    grade.sliders["temperature"].blockSignals(False)
    grade.update_preset()
    assert store.get("Warm Doc") == {"temperature": 25.0}
    grade.delete_preset()
    assert store.names() == [] and grade.look.findData("custom:Warm Doc") == -1
    assert grade.look.findData("builtin:cinematic") >= 0  # built-in looks remain


@pytest.fixture
def edit_window(qtbot, window, tmp_path):
    video = make_sample_video(tmp_path / "talk.mp4", seconds=3.0, size="320x180")
    window.media_page.import_paths([str(video)])
    item = window.controller.project.media[0]
    qtbot.waitUntil(lambda: item.metadata is not None, timeout=20000)
    window.controller.set_captions(item.id, CaptionTrack([CaptionSegment(0.2, 1.4, "ক্ষমা করো"),
                                                          CaptionSegment(1.6, 2.8, "second line")]))
    window.navigate("Edit")
    window.edit_page.show_tool("captions")
    return window, item


def test_edit_page_animation_tab_applies_previews_and_exports(qtbot, edit_window, tmp_path, monkeypatch):
    window, item = edit_window
    caps = window.edit_page.panels["captions"]
    assert [caps.tabs.tabText(i) for i in range(caps.tabs.count())] == ["Text", "Templates", "Style", "Animation"]
    caps.animation.entrance.setCurrentIndex(caps.animation.entrance.findData("pop"))
    caps.animation.word.setCurrentIndex(caps.animation.word.findData("karaoke"))
    caps.anim_selected_button.click()
    assert "Select captions" in caps.anim_status.text()
    caps.table.selectRow(0)
    caps.anim_selected_button.click()
    segs = item.captions.segments
    assert segs[0].animation["entrance"] == "pop" and segs[1].animation is None
    caps.table.selectRow(0)
    caps.anim_copy_button.click()
    caps.table.selectRow(1)
    caps.anim_paste_button.click()
    assert item.captions.segments[1].animation == item.captions.segments[0].animation
    caps.table.selectRow(1)
    caps.anim_clear_button.click()
    assert item.captions.segments[1].animation is None
    caps.animation.entrance.setCurrentIndex(caps.animation.entrance.findData("fade"))
    caps.animation.word.setCurrentIndex(caps.animation.word.findData("none"))
    caps.anim_all_button.click()
    assert all(s.animation["entrance"] == "fade" for s in item.captions.segments)
    caps.anim_reset_button.click()
    assert caps.animation.animation().entrance == "none"

    # preview: a real clip rendered with the same ASS as the export
    played = []
    monkeypatch.setattr(caption_dialogs.ClipPreviewDialog, "exec", lambda self: played.append(self) or 0)
    caps.table.selectRow(0)
    caps.animation.entrance.setCurrentIndex(caps.animation.entrance.findData("slide_up"))
    job = caps.preview_animation()
    assert "\\move(" in job.payload["ass"] and job.payload["end"] - job.payload["start"] <= 2.1
    qtbot.waitUntil(lambda: len(played) == 1, timeout=30000)
    assert Path(window.services.jobs.store.get(job.id).result["clip"]).is_file()
    assert item.captions.segments[0].animation["entrance"] == "fade"  # previewing does not apply

    # the Edit page export burns in the animated captions
    window.controller.update_resize(item.id, burn_captions=True, burn_set=True)
    window.edit_page.show_tool("final")
    export = window.edit_page.start_export(str(tmp_path / "animated.mp4"), OutputSettings(encoder="cpu",
                                                                                          quality="small"))
    assert r"\1a&HFF&" in export.payload["ass"] and r"\t(0," in export.payload["ass"]
    qtbot.waitUntil(lambda: window.services.jobs.store.get(export.id).state.is_finished, timeout=60000)
    assert window.services.jobs.store.get(export.id).state is JobState.COMPLETED

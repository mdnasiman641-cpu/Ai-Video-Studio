"""Bulk Processing: export modes, output size picker, the export preview, hardware detection,
queue details, folders, presets and the log (offscreen GUI; hardware detection faked)."""

import pytest
from PySide6.QtWidgets import QInputDialog, QMessageBox

from ai_video_studio.core import ffmpeg_commands as fc
from ai_video_studio.core import hardware
from ai_video_studio.jobs.models import JobState
from ai_video_studio.render.batch import RenderPreset
from ai_video_studio.render.layout import ResizeSettings
from ai_video_studio.render.output import OutputSettings
from ai_video_studio.services import Services
from ai_video_studio.ui.main_window import MainWindow
from ai_video_studio.ui.pages import bulk as bulk_module
from ai_video_studio.ui.pages.bulk import Q_MODE, Q_OUTPUT
from tests.conftest import make_sample_video, requires_ffmpeg

pytestmark = requires_ffmpeg


def fake_hardware(nvenc: bool):
    info = hardware.HardwareInfo(os="Windows 11", cpu_name="AMD Ryzen 7 7700 8-Core Processor", logical_cores=16,
                                 physical_cores=8, memory_gb=31.2,
                                 gpus=[hardware.GpuInfo("NVIDIA GeForce RTX 4070", "nvidia", 12282)],
                                 ffmpeg_version="7.1.1")
    for name in ("h264_nvenc", "hevc_nvenc", "h264_qsv", "hevc_qsv", "h264_amf", "hevc_amf"):
        ok = nvenc and name.endswith("_nvenc")
        info.encoders[name] = hardware.EncoderCheck(name, name.endswith("_nvenc"), ok,
                                                    "" if ok else "No capable devices found")
    return info


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    for name in ("warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(hardware, "detect", lambda ffmpeg, refresh=False: fake_hardware(True))
    monkeypatch.setattr(fc, "nvenc_usable", lambda ffmpeg, encoder="h264_nvenc": encoder.endswith("_nvenc"))
    services = Services(tmp_path / "ui.db")
    win = MainWindow(services)
    win.show()
    win.navigate("Bulk Processing")
    yield win
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Discard)
    win.close()
    win.deleteLater()
    services.close()


def add(qtbot, page, tmp_path, folders=("a", "a", "b")):
    paths = []
    for n, folder in enumerate(folders):
        (tmp_path / folder).mkdir(exist_ok=True)
        paths.append(str(make_sample_video(tmp_path / folder / f"v{n}.mp4", seconds=1.0, size="640x360")))
    page.add_paths(paths)
    qtbot.waitUntil(lambda: all(i.metadata is not None for i in page.batch.items), timeout=20000)
    page.output_dir.setText(str(tmp_path / "out"))
    page._on_output_changed()


def test_hardware_panel_marks_encoders_and_recommends(qtbot, window):
    page = window.bulk_page
    qtbot.waitUntil(lambda: page.hardware is not None, timeout=10000)
    text = page.hardware_label.text()
    assert "AMD Ryzen 7 7700" in text and "RTX 4070" in text and "h264_nvenc ✔ usable" in text
    assert "Recommended: Auto encoder (NVIDIA NVENC), 2 export(s) at a time" in text
    combo = page.editor.output.encoder
    qsv = combo.findData("qsv")
    assert combo.itemText(qsv).endswith("not available") and not combo.model().item(qsv).isEnabled()
    assert combo.model().item(combo.findData("nvenc")).isEnabled()
    page.use_recommended()
    assert window.services.settings.get("max_concurrent_renders") == 2
    assert window.services.jobs.max_heavy == 2


def test_export_preview_follows_aspect_resolution_mode_and_encoder(qtbot, window, tmp_path):
    page = window.bulk_page
    add(qtbot, page, tmp_path, ("a",))
    qtbot.waitUntil(lambda: page.hardware is not None, timeout=10000)
    e = page.editor
    e.set_preset(RenderPreset(resize=ResizeSettings(preset="1080x1920", mode="blur"),
                              filename_template="{original_name}_{resolution}"))
    e.size.aspect.setCurrentIndex(e.size.aspect.findData("4:5"))
    e.size.resolution.setCurrentIndex(e.size.resolution.findData(1080))
    e.output.mode.setCurrentIndex(e.output.mode.findData("high"))
    page._on_shared_changed()
    text = page.resolved.text()
    for part in ("Aspect ratio:</b> 4:5", "Output resolution:</b> 1080 × 1350", "Resolution preset:</b> 1080p",
                 "Quality:</b> High Quality", "Encoder:</b> NVIDIA NVENC H.264",
                 "Processing mode:</b> GPU-accelerated encoding", "File name:</b> v0_1080x1350.mp4"):
        assert part in text, text
    assert page.batch.shared.resize.output_size(640, 360) == (1080, 1350)  # the same values the export uses
    assert "upscaled" in text  # 640×360 source
    # custom size, the CPU encoder and MKV
    e.size.aspect.setCurrentIndex(e.size.aspect.findData("custom"))
    e.size.width_spin.setValue(1000)
    e.size.height_spin.setValue(500)
    e.output.encoder.setCurrentIndex(e.output.encoder.findData("cpu"))
    e.output.container.setCurrentIndex(e.output.container.findData("mkv"))
    page._on_shared_changed()
    text = page.resolved.text()
    assert "1000 × 500" in text and "CPU (libx264)" in text and "CPU encoding" in text and ".mkv" in text
    e.size.width_spin.setValue(1001)  # odd sizes are not offered
    assert e.size.width_spin.value() == 1002
    e.output.mode.setCurrentIndex(e.output.mode.findData("draft"))
    page._on_shared_changed()
    assert "not for final delivery" in page.resolved.text()
    assert not e.output.crf.isEnabled() and "CRF/CQ 30" in e.output.mode_note.text()
    e.output.mode.setCurrentIndex(e.output.mode.findData("custom"))
    assert e.output.cpu_preset.isEnabled() and e.output.audio_bitrate.isEnabled()


def test_folders_presets_queue_details_cancel_and_log(qtbot, window, tmp_path, monkeypatch):
    page = window.bulk_page
    add(qtbot, page, tmp_path)
    page.select_folder(str(tmp_path / "b"))
    assert [i.selected for i in page.batch.items] == [False, False, True]
    page._fill_folder_menu()
    assert len(page.folder_menu.actions()) == 2
    # duplicate a built-in preset
    page.preset_combo.setCurrentIndex(page.preset_combo.findData("YouTube 1920x1080"))
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("YouTube copy", True))
    assert page.duplicate_preset() == "YouTube copy"
    assert window.services.render_presets.get("YouTube copy").resize.preset == "1920x1080"
    # queue: output size and mode columns, real job state, cancel the rest of the batch
    page.select_folder(str(tmp_path / "a"))
    page.editor.set_preset(RenderPreset(resize=ResizeSettings(preset="custom", width=320, height=320, mode="fit"),
                                        output=OutputSettings(mode="draft", encoder="cpu")))
    page._on_shared_changed()
    window.services.jobs.pause()
    jobs = page.start()
    assert len(jobs) == 2 and page.start() == []  # a second click starts nothing again
    page.refresh()
    assert page.queue_table.item(0, Q_OUTPUT).text() == "320×320 (1:1)"
    assert page.queue_table.item(0, Q_MODE).text() == "Draft / Fast Preview"
    page.cancel_batch()
    assert all(window.services.jobs.store.get(j.id).state is JobState.CANCELLED for j in jobs)
    page.queue_table.selectRow(0)
    log = page.job_log(page.batch.items[0])
    assert "State: cancelled" in log and "Settings: Resize Custom size, fit; Draft / Fast Preview" in log, log
    shown = []
    monkeypatch.setattr(bulk_module.LogDialog, "exec", lambda self: shown.append(self.text) or 0)
    page.show_log()
    assert shown and "Application log folder" in shown[0]


def test_live_details_and_estimates(window):
    page = window.bulk_page
    from ai_video_studio.jobs.models import Job

    job = Job(id="j1", kind="render_video", title="t", payload={}, state=JobState.RUNNING, progress=0.5,
              stage="rendering", started_at="2020-01-01T00:00:00+00:00")
    window.services.jobs._details["j1"] = {"step": "Rendering and encoding", "encoder": "NVIDIA NVENC H.264",
                                            "speed": 3.2}
    text = page.live_text(job)
    assert text.startswith("Rendering and encoding") and "NVIDIA NVENC H.264" in text and "3.20× realtime" in text
    assert "left" in text and page.remaining_estimate(job, 100.0) == 100.0
    assert page.remaining_estimate(Job(id="j2", kind="k", title="t", payload={}, state=JobState.RUNNING,
                                        progress=0.01), 100.0) is None
    window.services.jobs._details.clear()
    assert bulk_module.format_seconds(3725) == "1h 02m" and bulk_module.format_seconds(65) == "1m 05s"

"""The Edit page Final Export with every operation switched on, as configured in the app:
Smart Cut (real audio analysis), 82-style caption list in Highlight Style, 1080×1920 Blur
background with Auto Reframe (Auto Face Tracking, stored analysis), speed 1.25× and audio
processing. A real FFmpeg export is run and the output file is checked (offscreen GUI)."""

import subprocess

import pytest
from PySide6.QtWidgets import QMessageBox

from ai_video_studio.captions.model import CaptionSegment, CaptionTrack
from ai_video_studio.core.ffmpeg_tools import find_tool, probe_video
from ai_video_studio.jobs import render_jobs
from ai_video_studio.jobs.models import JobState
from ai_video_studio.reframe.model import ReframeState, Track, TrackingAnalysis, TrackSample
from ai_video_studio.render.output import OutputSettings
from ai_video_studio.services import Services
from ai_video_studio.ui.main_window import MainWindow
from tests.conftest import requires_ffmpeg

pytestmark = requires_ffmpeg
SECONDS, FPS = 12, 24
ON = "lt(mod(t,3),2)"  # sound and a white square for 2 s, then 1 s of silence, repeated


@pytest.fixture
def movie(tmp_path_factory):
    path = tmp_path_factory.mktemp("final") / "movie 720p.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=0x404040:s=1280x720:r={FPS}:d={SECONDS}",
        "-f", "lavfi", "-i", f"aevalsrc='0.5*sin(2*PI*330*t)*{ON}':s=48000:d={SECONDS}",
        "-filter_complex", f"[0:v]drawbox=x=100:y=200:w=200:h=200:t=fill:c=white:enable='{ON}'[v]",
        "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
        check=True)
    return path


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch, movie):
    for name in ("warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    services = Services(tmp_path / "ui.db")
    win = MainWindow(services)
    win.show()
    win.media_page.import_paths([str(movie)])
    qtbot.waitUntil(lambda: win.controller.project.media[0].metadata is not None, timeout=20000)
    yield win
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Discard)
    win.close()
    win.deleteLater()
    services.close()


def frame_rgb(path, at):
    return subprocess.run([find_tool("ffmpeg"), "-v", "error", "-ss", f"{at:.3f}", "-i", str(path), "-frames:v", "1",
                           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout


def yellow(raw: bytes) -> int:
    return sum(1 for i in range(0, len(raw), 3) if raw[i] > 200 and raw[i + 1] > 200 and raw[i + 2] < 80)


def test_every_enabled_operation_is_in_the_exported_video(qtbot, window, tmp_path, monkeypatch):
    graphs = []
    original_args = render_jobs.render_args
    monkeypatch.setattr(render_jobs, "render_args", lambda *a, **k: graphs.append(a[2]) or original_args(*a, **k))
    c = window.controller
    item = c.project.media[0]
    window.navigate("Edit")
    page = window.edit_page

    # 1 Smart Cut: the real audio analysis, then the detected cuts are applied in the Smart Cut tool
    page.show_tool("smart_cut")
    page.panels["smart_cut"].analyse_button.click()
    qtbot.waitUntil(lambda: item.smart_cut is not None and item.smart_cut.levels is not None, timeout=60000)
    page.panels["smart_cut"].apply_button.click()
    cuts = c.applied_cuts(item.id)
    assert cuts and len(cuts) == 4  # four 2 s sound parts, three silences removed

    # 2 Captions: one per sound part, Highlight Style
    segments = [CaptionSegment(start + 0.2, start + 1.6, f"line {n}") for n, start in enumerate(range(0, 12, 3))]
    c.set_captions(item.id, CaptionTrack(segments))
    page.show_tool("captions")
    caps = page.panels["captions"]
    caps.templates.buttons["Highlight Style"].click()
    caps.apply_to_all()
    assert caps.burn.isChecked()

    # 3 Resize / Crop: 1080×1920, Blur background
    page.show_tool("resize")
    resize = page.panels["resize"]
    assert resize.size.currentData() == "1080x1920" and not resize.enabled.isChecked()  # shown, but off
    resize.size.activated.emit(resize.size.findData("1080x1920"))  # the user picks the size already shown
    resize.mode.activated.emit(resize.mode.findData("blur"))
    assert resize.enabled.isChecked() and c.final_operations(item.id)["use_resize"]

    # 4 Auto Reframe: Auto Face Tracking with a stored analysis (a face moving left to right)
    face = Track(1, "face", [TrackSample(t / 2, 0.1 + 0.03 * t, 0.3, 0.15, 0.27, 0.95) for t in range(SECONDS * 2)])
    settings = c.reframe_state(item.id).settings
    settings.tracking_mode = "auto_face"
    c.set_reframe_settings(item.id, settings)
    item.reframe = ReframeState(settings, TrackingAnalysis(1280, 720, SECONDS, 2.0, SECONDS * 2, "test",
                                                           tracks=[face], features=["faces", "persons"]))
    page.show_tool("reframe")
    page.panels["reframe"].use.setChecked(True)
    assert "Ready" in page.panels["reframe"].status.text()

    # 5 Speed 1.25×, audio follows, pitch preserved; 6 Audio: volume 120%, light noise reduction
    form = page.effects_form
    form.speed_enabled.setChecked(True)
    form.video_speed.setValue(1.25)
    form.audio_enabled.setChecked(True)
    form.volume.setValue(120)
    form.noise.setCurrentIndex(form.noise.findData("light"))

    page.show_tool("final")
    summary = page.summary_html(item)
    for part in ("✔ <b>Smart Cut</b>: enabled — 4 kept part(s)", "✔ <b>Captions</b>: enabled — 4 caption(s), "
                 "Highlight Style", "✔ <b>Framing</b>: 1080×1920 (9:16), Auto Reframe",
                 "✔ <b>Auto Reframe</b>: enabled — Auto Face Tracking", "✔ <b>Speed</b>: 1.25×",
                 "✔ <b>Audio</b>: enabled — volume 120%"):
        assert part in summary, summary

    output = tmp_path / "final.mp4"
    job = page.start_export(str(output), OutputSettings(encoder="cpu", quality="small"))
    qtbot.waitUntil(lambda: window.services.jobs.store.get(job.id).state.is_finished, timeout=180000)
    done = window.services.jobs.store.get(job.id)
    assert done.state is JobState.COMPLETED, done.error

    # the FFmpeg filter graph: cuts → Auto Reframe crop (moving, sendcmd) → speed → captions; audio filters
    graph = graphs[-1]
    order = [graph.index(s) for s in ("concat=n=4", "sendcmd=f=", "scale=1080:1920", "setpts=PTS/1.25",
                                      "subtitles=captions.ass")]
    assert order == sorted(order), graph
    assert "atempo=1.25" in graph and "afftdn=nr=6" in graph and "volume=1.2" in graph

    # the exported file
    kept = sum(b - a for a, b in cuts)
    meta = probe_video(str(output), find_tool("ffprobe"))
    assert (meta.width, meta.height) == (1080, 1920) and meta.has_audio
    assert abs(meta.duration - kept / 1.25) < 0.15, (meta.duration, kept)
    # captions: each line is shown at its time on the exported timeline (cuts, then 1.25×) with its yellow box
    from ai_video_studio.captions.model import output_captions

    expected = output_captions(item.captions.segments, cuts, 1.25)
    assert [s.text for s in expected] == ["line 0", "line 1", "line 2", "line 3"]
    for seg in expected:
        assert yellow(frame_rgb(output, (seg.start + seg.end) / 2)) > 2000, seg
    gap = (expected[0].end + expected[1].start) / 2
    assert yellow(frame_rgb(output, gap)) < 100  # between two captions
    assert output.is_file() and item.path != str(output)

"""Resize & Crop against real FFmpeg: the exported pixels must match the computed layout.

The source is red on the left half and blue on the right half, so every mode's crop,
scale, position and background can be checked in the output frame.
"""

import subprocess

import pytest

from ai_video_studio.captions.ass import build_ass
from ai_video_studio.captions.model import CaptionSegment
from ai_video_studio.captions.style import CaptionStyle
from ai_video_studio.core.ffmpeg_tools import find_tool, probe_video
from ai_video_studio.jobs.manager import JobManager
from ai_video_studio.jobs.models import JobState
from ai_video_studio.jobs.render_jobs import RENDER_FRAME_JOB, RENDER_JOB, make_render_frame_handler, make_render_handler
from ai_video_studio.jobs.store import JobStore
from ai_video_studio.reframe.framing import CropPath
from ai_video_studio.render.layout import ResizeSettings, compute_layout
from ai_video_studio.render.output import OutputSettings
from ai_video_studio.render.stages import mapped_sendcmd_script, source_time_mapper
from tests.conftest import requires_ffmpeg

np = pytest.importorskip("numpy")
pytestmark = requires_ffmpeg
W, H = 320, 180


def ffmpeg():
    return find_tool("ffmpeg")


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    path = tmp_path_factory.mktemp("resize") / "halves.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=blue:s={W}x{H}:r=25:d=2",
                    "-f", "lavfi", "-i", f"color=c=red:s={W // 2}x{H}:r=25:d=2", "-f", "lavfi", "-i", "sine=d=2",
                    "-filter_complex", "[0:v][1:v]overlay=0:0[v]", "-map", "[v]", "-map", "2:a",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "2", str(path)], check=True)
    return path


@pytest.fixture
def manager(db):
    m = JobManager(JobStore(db), 2)
    m.register(RENDER_JOB, make_render_handler(ffmpeg))
    m.register(RENDER_FRAME_JOB, make_render_frame_handler(ffmpeg))
    yield m
    m.shutdown()


def export(manager, clip, output, settings: ResizeSettings, ass=None, keep=None):
    layout = compute_layout(W, H, settings)
    payload = {"path": str(clip), "media_id": "m", "output": str(output), "duration": 2.0, "fps": 25,
               "has_audio": True, "audio_codec": "aac", "keep_ranges": keep, "ass": ass,
               "stage": {"type": "layout", "layout": layout.to_dict()},
               "output_settings": OutputSettings(encoder="cpu", quality="high").to_dict()}
    job = manager.submit(RENDER_JOB, "resize", payload)
    assert manager.wait_idle(60)
    job = manager.store.get(job.id)
    assert job.state is JobState.COMPLETED, job.error
    return layout


def frame(path, w, h, at=1.0):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(h, w, 3).astype(int)


def is_red(px):
    return px[0] > 180 and px[1] < 70 and px[2] < 70


def is_blue(px):
    return px[2] > 180 and px[0] < 70 and px[1] < 70


def test_fit_into_vertical_canvas_matches_layout(manager, clip, tmp_path):
    out = tmp_path / "fit.mp4"
    layout = export(manager, clip, out, ResizeSettings(preset="custom", width=180, height=320, mode="fit",
                                                       background="#00ff00"))
    meta = probe_video(str(out), find_tool("ffprobe"))
    assert (meta.width, meta.height) == (180, 320) and meta.has_audio
    img = frame(out, 180, 320)
    top = layout.dst_y
    assert is_red(img[top + layout.dst_h // 2, layout.dst_x + layout.dst_w // 4])
    assert is_blue(img[top + layout.dst_h // 2, layout.dst_x + 3 * layout.dst_w // 4])
    assert img[top // 2, 90][1] > 180 and img[top // 2, 90][0] < 80  # green padding above the video
    assert img[top + layout.dst_h + 10, 90][1] > 180  # and below it


def test_fill_follows_position(manager, clip, tmp_path):
    out = tmp_path / "fill.mp4"
    export(manager, clip, out, ResizeSettings(preset="custom", width=90, height=180, mode="fill", pos_x=0.0))
    img = frame(out, 90, 180)
    assert all(is_red(img[y, x]) for y in (10, 90, 170) for x in (5, 45, 80))  # only the red half is visible


def test_blur_background_is_not_flat(manager, clip, tmp_path):
    out = tmp_path / "blur.mp4"
    layout = export(manager, clip, out, ResizeSettings(preset="custom", width=180, height=320, mode="blur", blur=10))
    img = frame(out, 180, 320)
    above = img[: layout.dst_y - 4]
    assert above.max() > 100  # the blurred copy of the video, not black
    assert is_red(img[layout.dst_y + layout.dst_h // 2, layout.dst_x + 20])


def test_exact_frame_preview_equals_the_exported_frame(manager, clip, tmp_path):
    settings = ResizeSettings(preset="custom", width=240, height=240, mode="custom", zoom=1.4, pos_x=0.3, pos_y=0.6)
    out = tmp_path / "custom.mp4"
    layout = export(manager, clip, out, settings)
    job = manager.submit(RENDER_FRAME_JOB, "preview", {"path": str(clip), "media_id": "m", "at": 1.0,
                                                       "stage": {"type": "layout", "layout": layout.to_dict()}})
    assert manager.wait_idle(60)
    job = manager.store.get(job.id)
    assert job.state is JobState.COMPLETED, job.error
    png = subprocess.run(["ffmpeg", "-v", "error", "-i", job.result["image"], "-f", "rawvideo", "-pix_fmt", "rgb24",
                          "-"], check=True, capture_output=True).stdout
    preview = np.frombuffer(png, np.uint8).reshape(240, 240, 3).astype(int)
    exported = frame(out, 240, 240)
    assert np.abs(preview - exported).mean() < 6  # only compression differences


def test_captions_are_drawn_on_the_output_canvas(manager, clip, tmp_path):
    settings = ResizeSettings(preset="custom", width=180, height=320, mode="fit")
    plain, captioned = tmp_path / "plain.mp4", tmp_path / "captioned.mp4"
    layout = export(manager, clip, plain, settings)
    ass = build_ass([CaptionSegment(0.0, 2.0, "HELLO")], CaptionStyle(font_size=90, margin_v=8), 180, 320)
    export(manager, clip, captioned, settings, ass=ass)
    diff = np.abs(frame(captioned, 180, 320) - frame(plain, 180, 320)).sum(axis=2)
    rows = np.nonzero(diff.max(axis=1) > 100)[0]
    assert len(rows) and rows.min() > layout.dst_y + layout.dst_h  # bottom caption sits in the lower padding


def test_resize_combined_with_cuts(manager, clip, tmp_path):
    out = tmp_path / "cut.mp4"
    export(manager, clip, out, ResizeSettings(preset="custom", width=160, height=160, mode="fit"),
           keep=[[0.0, 0.6], [1.2, 2.0]])
    meta = probe_video(str(out), find_tool("ffprobe"))
    assert (meta.width, meta.height) == (160, 160) and abs(meta.duration - 1.4) < 0.1


def test_reframe_crop_is_mapped_through_cuts():
    path = CropPath.from_keyframes(640, 360, 200, 360, [[0.0, 0, 0], [10.0, 440, 0]])
    to_source = source_time_mapper([(0.0, 2.0), (6.0, 10.0)])
    assert to_source(1.0) == 1.0 and to_source(2.5) == 6.5 and to_source(99) == 10.0
    script = mapped_sendcmd_script(path, 25, 6.0, to_source)
    # 2 s into the edited video the source is at 6 s, so the crop jumps to x = 264.
    line = next(ln for ln in script.splitlines() if ln.startswith("1.98"))
    assert "crop x 264" in line


def test_moving_reframe_stage_with_cuts_exports(manager, clip, tmp_path):
    # A crop that pans from the red half to the blue half over the clip.
    stage = {"type": "reframe", "src_w": W, "src_h": H, "crop_w": 100, "crop_h": 180,
             "keyframes": [[0.0, 0, 0], [2.0, 220, 0]], "out_w": 100, "out_h": 180, "fps": 25, "duration": 2.0}
    out = tmp_path / "reframed.mp4"
    payload = {"path": str(clip), "media_id": "m", "output": str(out), "duration": 2.0, "fps": 25,
               "has_audio": True, "audio_codec": "aac", "keep_ranges": [[0.0, 0.4], [1.6, 2.0]], "stage": stage,
               "output_settings": OutputSettings(encoder="cpu").to_dict()}
    job = manager.submit(RENDER_JOB, "reframe", payload)
    assert manager.wait_idle(60)
    job = manager.store.get(job.id)
    assert job.state is JobState.COMPLETED, job.error
    assert is_red(frame(out, 100, 180, at=0.1)[90, 50])    # start: crop on the left (red) half
    assert is_blue(frame(out, 100, 180, at=0.7)[90, 50])   # after the cut the source is at 1.9 s: blue half

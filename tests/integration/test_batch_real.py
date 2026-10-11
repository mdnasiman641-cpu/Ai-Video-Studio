"""Batch exports against real FFmpeg: resize items, an Auto Reframe item and output protection."""

import subprocess
from pathlib import Path

import pytest

from ai_video_studio.core.ffmpeg_tools import find_tool, probe_video
from ai_video_studio.jobs.manager import JobManager
from ai_video_studio.jobs.models import JobState
from ai_video_studio.jobs.render_jobs import RENDER_JOB, make_render_handler
from ai_video_studio.jobs.store import JobStore
from ai_video_studio.reframe.detection import DetectorSet
from ai_video_studio.reframe.model import ReframeSettings
from ai_video_studio.render.batch import Batch, RenderPreset, build_payload, output_name
from ai_video_studio.render.layout import ResizeSettings
from ai_video_studio.render.naming import path_key, plan_outputs
from ai_video_studio.render.output import OutputSettings
from tests.conftest import requires_ffmpeg
from tests.integration.test_reframe_jobs_real import red_detector

pytestmark = requires_ffmpeg


def ffmpeg():
    return find_tool("ffmpeg")


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    folder = tmp_path_factory.mktemp("batch")
    square = folder / "square.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x303030:s=640x360:r=25:d=2",
                    "-f", "lavfi", "-i", "color=c=red:s=50x50:r=25:d=2", "-f", "lavfi", "-i", "sine=d=2",
                    "-filter_complex", "[0:v][1:v]overlay=x='60+200*t':y=140[v]", "-map", "[v]", "-map", "2:a",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "2", str(square)], check=True)
    plain = folder / "plain clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=480x270:r=25:d=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(plain)], check=True)
    return square, plain


@pytest.fixture
def manager(db):
    m = JobManager(JobStore(db), max_concurrent=3, max_heavy=1)
    m.register(RENDER_JOB, make_render_handler(ffmpeg, lambda p: DetectorSet(red_detector, None, "red", [])),
               heavy=True)
    yield m
    m.shutdown()


def probe(path):
    return probe_video(str(path), find_tool("ffprobe"))


def test_batch_of_resize_and_reframe_items(manager, clips, tmp_path):
    square, plain = clips
    batch = Batch(output_dir=str(tmp_path))
    batch.add([str(square), str(plain)])
    for item in batch.items:
        item.metadata = probe(item.path)
    batch.shared = RenderPreset(resize=ResizeSettings(preset="custom", width=320, height=320, mode="blur"),
                                output=OutputSettings(encoder="cpu", quality="small"),
                                filename_template="{original_name}_{resolution}")
    batch.items[0].override = RenderPreset(operation="reframe", tracking_mode="auto_face",
                                           resize=ResizeSettings(preset="custom", width=180, height=320),
                                           output=OutputSettings(encoder="cpu", quality="small"),
                                           filename_template="{original_name}_reframed")
    planned = [(i.id, Path(batch.output_dir) / output_name(batch.effective(i), i, n + 1, "test"))
               for n, i in enumerate(batch.items)]
    decisions = plan_outputs(planned, "rename", {path_key(i.path) for i in batch.items}, set())
    jobs = []
    for item, decision in zip(batch.items, decisions):
        payload, _notes = build_payload(item, batch.effective(item), decision.path, overwrite=False, batch_id=batch.id,
                                        captions=None, caption_style=None, tracking_defaults=ReframeSettings(),
                                        models_dir=str(tmp_path))
        jobs.append(manager.submit(RENDER_JOB, item.name, payload))
    stages = set()
    manager.add_listener(lambda job: job.stage and stages.add(job.stage))
    assert manager.wait_idle(120)
    for job in jobs:
        done = manager.store.get(job.id)
        assert done.state is JobState.COMPLETED, done.error
        assert done.result["batch_id"] == batch.id
    reframed, resized = probe(tmp_path / "square_reframed.mp4"), probe(tmp_path / "plain clip_320x320.mp4")
    assert (reframed.width, reframed.height) == (180, 320) and reframed.has_audio
    assert (resized.width, resized.height) == (320, 320) and not resized.has_audio
    assert {"analyzing", "rendering"} <= stages


def test_existing_outputs_are_renamed_not_overwritten(clips, tmp_path):
    _square, plain = clips
    existing = tmp_path / "plain clip_youtube.mp4"
    existing.write_bytes(b"precious")
    decisions = plan_outputs([("a", existing)], "rename", {path_key(plain)}, set())
    assert decisions[0].path.name == "plain clip_youtube (2).mp4" and existing.read_bytes() == b"precious"

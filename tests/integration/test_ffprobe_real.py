"""Integration tests against real FFprobe using generated sample videos."""

import threading

import pytest

from ai_video_studio.core.errors import JobCancelled, MediaError
from ai_video_studio.core.ffmpeg_tools import find_tool, probe_video, tool_version
from ai_video_studio.core.process import run_process
from ai_video_studio.jobs.handlers import PROBE_JOB, make_probe_handler
from ai_video_studio.jobs.manager import JobManager
from ai_video_studio.jobs.models import JobState
from ai_video_studio.jobs.store import JobStore
from tests.conftest import requires_ffmpeg

pytestmark = requires_ffmpeg


def test_tools_detected():
    ffprobe = find_tool("ffprobe")
    assert ffprobe is not None
    assert "ffprobe version" in tool_version(ffprobe)


def test_probe_landscape_with_audio(sample_videos):
    meta = probe_video(sample_videos["landscape"], find_tool("ffprobe"))
    assert (meta.width, meta.height) == (320, 180)
    assert meta.fps == 25
    assert meta.duration == pytest.approx(1.0, abs=0.1)
    assert meta.video_codec == "h264"
    assert meta.has_audio and meta.audio_codec == "aac"


def test_probe_vertical_without_audio(sample_videos):
    meta = probe_video(sample_videos["vertical"], find_tool("ffprobe"))
    assert meta.resolution_text == "180x320"
    assert not meta.has_audio


def test_probe_rejects_non_video(tmp_path):
    bogus = tmp_path / "fake.mp4"
    bogus.write_text("this is not a video")
    with pytest.raises(MediaError, match="could not read"):
        probe_video(bogus, find_tool("ffprobe"))
    with pytest.raises(MediaError, match="not found"):
        probe_video(tmp_path / "missing.mp4", find_tool("ffprobe"))


def test_run_process_cancellation():
    event = threading.Event()
    event.set()
    with pytest.raises(JobCancelled):
        run_process(["ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i", "testsrc", "-f", "null", "-"],
                    cancel_event=event)


def test_probe_job_end_to_end(db, sample_videos, tmp_path):
    manager = JobManager(JobStore(db), 2)
    manager.register(PROBE_JOB, make_probe_handler(lambda: find_tool("ffprobe")))
    good = manager.submit(PROBE_JOB, "good", {"path": str(sample_videos["landscape"]), "media_id": "m1"})
    bad_file = tmp_path / "bad.mp4"
    bad_file.write_text("x")
    bad = manager.submit(PROBE_JOB, "bad", {"path": str(bad_file), "media_id": "m2"})
    assert manager.wait_idle(20)
    good = manager.store.get(good.id)
    assert good.state is JobState.COMPLETED
    assert good.result["media_id"] == "m1" and good.result["metadata"]["width"] == 320
    bad = manager.store.get(bad.id)
    assert bad.state is JobState.FAILED and "could not read" in bad.error
    manager.shutdown()

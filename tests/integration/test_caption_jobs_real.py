"""Caption jobs against real FFmpeg/libass. Whisper itself is replaced by a fake model
(downloading a real model is not part of the normal test run; see test_whisper_real.py)."""

import subprocess
import threading

import pytest

from ai_video_studio.captions import transcriber
from ai_video_studio.captions.ass import build_ass
from ai_video_studio.captions.model import CaptionSegment
from ai_video_studio.captions.style import BUILTIN_PRESETS, CaptionStyle
from ai_video_studio.core.ffmpeg_tools import find_tool, probe_video
from ai_video_studio.jobs.caption_jobs import (
    BURN_IN_JOB,
    FRAME_PREVIEW_JOB,
    TRANSCRIBE_JOB,
    make_burn_in_handler,
    make_frame_preview_handler,
    make_transcribe_handler,
)
from ai_video_studio.jobs.manager import JobManager
from ai_video_studio.jobs.models import JobState
from ai_video_studio.jobs.store import JobStore
from tests.conftest import requires_ffmpeg
from tests.fakes import FakeFactory, FakeWhisperModel, fake_segments

pytestmark = requires_ffmpeg


def ffmpeg():
    return find_tool("ffmpeg")


@pytest.fixture(scope="module")
def blue_clip(tmp_path_factory):
    """Plain blue 640x360 clip with a tone: subtitles are easy to detect by pixel difference."""
    path = tmp_path_factory.mktemp("cap") / "blue clip.mp4"  # space in name on purpose
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=640x360:r=25:d=3",
                    "-f", "lavfi", "-i", "sine=frequency=500:duration=3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


@pytest.fixture
def manager(db):
    transcriber.clear_model_cache()
    m = JobManager(JobStore(db), 2)
    yield m
    m.shutdown()


def gray_frame(path, at, w=640, h=360):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}", "-"],
                         check=True, capture_output=True).stdout
    assert len(raw) == w * h
    return raw


def region_diff(a, b, w, y0, y1):
    return sum(abs(a[y * w + x] - b[y * w + x]) for y in range(y0, y1) for x in range(0, w, 2)) / ((y1 - y0) * w / 2)


def test_transcribe_job_extracts_audio_and_builds_track(manager, blue_clip):
    model = FakeWhisperModel(fake_segments([[(0.2, 0.8, " Hello"), (0.9, 1.5, " there")]]), language="bn")
    manager.register(TRANSCRIBE_JOB, make_transcribe_handler(ffmpeg, model_factory=FakeFactory(model), cuda_devices=0))
    job = manager.submit(TRANSCRIBE_JOB, "t", {"path": str(blue_clip), "media_id": "m", "language": "bn",
                                               "duration": 3.0})
    assert manager.wait_idle(60)
    job = manager.store.get(job.id)
    assert job.state is JobState.COMPLETED, job.error
    assert job.result["track"]["segments"][0]["text"] == "Hello there"
    assert job.result["device"] == "cpu" and job.result["language"] == "bn"
    wav_path = model.calls[0][0]
    assert wav_path.endswith("audio.wav")
    import os
    assert not os.path.exists(wav_path)  # temp audio cleaned up


def test_transcribe_job_without_audio_fails_clearly(manager, sample_videos):
    model = FakeWhisperModel([])
    manager.register(TRANSCRIBE_JOB, make_transcribe_handler(ffmpeg, model_factory=FakeFactory(model), cuda_devices=0))
    job = manager.submit(TRANSCRIBE_JOB, "t", {"path": str(sample_videos["vertical"]), "media_id": "m"})
    manager.wait_idle(60)
    job = manager.store.get(job.id)
    assert job.state is JobState.FAILED and "no audio track" in job.error


def test_burn_in_renders_captions(manager, blue_clip, tmp_path):
    manager.register(BURN_IN_JOB, make_burn_in_handler(ffmpeg))
    style = CaptionStyle(font_family="DejaVu Sans", font_size=90, text_color="#FFFF00", outline_width=4)
    ass = build_ass([CaptionSegment(1.0, 2.0, "CAPTION TEST")], style, 640, 360)
    out = tmp_path / "out dir" / "captioned.mp4"
    job = manager.submit(BURN_IN_JOB, "burn", {"path": str(blue_clip), "output": str(out), "ass": ass,
                                               "duration": 3.0, "audio_codec": "aac", "has_audio": True,
                                               "encoder": "libx264", "quality": 18})
    assert manager.wait_idle(120)
    job = manager.store.get(job.id)
    assert job.state is JobState.COMPLETED, job.error
    assert job.result["encoder"] == "libx264" and out.is_file()
    assert not list(out.parent.glob("*.part*"))
    meta = probe_video(out, find_tool("ffprobe"))
    assert meta.resolution_text == "640x360" and meta.has_audio and abs(meta.duration - 3.0) < 0.2

    src_mid, out_mid = gray_frame(blue_clip, 1.5), gray_frame(out, 1.5)
    src_end, out_end = gray_frame(blue_clip, 2.6), gray_frame(out, 2.6)
    bottom = (220, 345)
    assert region_diff(src_mid, out_mid, 640, *bottom) > 2      # caption visible while active
    assert region_diff(src_mid, out_mid, 640, 0, 120) < 1       # top untouched
    assert region_diff(src_end, out_end, 640, *bottom) < 1      # gone after its end time


def test_burn_in_refuses_to_overwrite_source(manager, blue_clip):
    manager.register(BURN_IN_JOB, make_burn_in_handler(ffmpeg))
    job = manager.submit(BURN_IN_JOB, "burn", {"path": str(blue_clip), "output": str(blue_clip), "ass": "",
                                               "encoder": "libx264"})
    manager.wait_idle(30)
    assert "different from the source" in manager.store.get(job.id).error


def test_burn_in_cancel_removes_partial_output(manager, tmp_path):
    long_clip = tmp_path / "long.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=60",
                    "-c:v", "libx264", "-preset", "ultrafast", str(long_clip)], check=True)
    manager.register(BURN_IN_JOB, make_burn_in_handler(ffmpeg))
    ass = build_ass([CaptionSegment(0, 60, "x")], CaptionStyle(), 1280, 720)
    out = tmp_path / "cancelled.mp4"
    started = threading.Event()
    manager.add_listener(lambda j: started.set() if j.progress > 0 else None)
    job = manager.submit(BURN_IN_JOB, "burn", {"path": str(long_clip), "output": str(out), "ass": ass,
                                               "duration": 60, "has_audio": False, "encoder": "libx264"})
    assert started.wait(60)
    manager.cancel(job.id)
    assert manager.wait_idle(30)
    assert manager.store.get(job.id).state is JobState.CANCELLED
    assert not out.exists() and not list(tmp_path.glob("*.part*"))


def test_frame_preview(manager, blue_clip):
    manager.register(FRAME_PREVIEW_JOB, make_frame_preview_handler(ffmpeg))
    ass = build_ass([CaptionSegment(0, 3, "Boxed")], BUILTIN_PRESETS["Boxed"], 640, 360)
    job = manager.submit(FRAME_PREVIEW_JOB, "p", {"path": str(blue_clip), "ass": ass, "at": 1.0})
    manager.wait_idle(60)
    job = manager.store.get(job.id)
    assert job.state is JobState.COMPLETED, job.error
    from pathlib import Path

    assert Path(job.result["image"]).stat().st_size > 1000

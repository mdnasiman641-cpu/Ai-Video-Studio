"""Smart Cut against real FFmpeg: audio analysis, silence detection and edited export.

The test clip alternates one second of tone with one second of silence, and the picture is
white exactly while the tone plays. After the silences are cut, every tone onset in the
output must still coincide with a white-frame onset: this checks audio/video sync across
several cuts, frame snapping and the trim/concat graph.
"""

import subprocess
import threading
import time

import pytest

from ai_video_studio.captions.ass import build_ass
from ai_video_studio.captions.model import CaptionSegment, remap_to_cuts
from ai_video_studio.captions.style import CaptionStyle
from ai_video_studio.core.ffmpeg_tools import find_tool, probe_video
from ai_video_studio.jobs.manager import JobManager
from ai_video_studio.jobs.models import JobState
from ai_video_studio.jobs.render_jobs import (
    RENDER_JOB,
    SMARTCUT_ANALYZE_JOB,
    make_render_handler,
    make_smartcut_analyze_handler,
)
from ai_video_studio.jobs.store import JobStore
from ai_video_studio.render.output import OutputSettings
from ai_video_studio.smartcut.levels import AudioLevels
from ai_video_studio.smartcut.model import SmartCutSettings, SmartCutState
from tests.conftest import requires_ffmpeg

np = pytest.importorskip("numpy")
pytestmark = requires_ffmpeg

FPS, SECONDS = 25, 8
ON = "lt(mod(t,2),1)"  # tone and white picture during the first second of every two


def ffmpeg():
    return find_tool("ffmpeg")


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    path = tmp_path_factory.mktemp("smartcut") / "tone gaps.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=black:s=160x90:r={FPS}:d={SECONDS}",
        "-f", "lavfi", "-i", f"aevalsrc='0.5*sin(2*PI*440*t)*{ON}':s=48000:d={SECONDS}",
        "-filter_complex", f"[0:v]drawbox=t=fill:c=white:enable='{ON}'[v]",
        "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-t", str(SECONDS), str(path)], check=True)
    return path


@pytest.fixture
def manager(db):
    m = JobManager(JobStore(db), 2)
    m.register(SMARTCUT_ANALYZE_JOB, make_smartcut_analyze_handler(ffmpeg))
    m.register(RENDER_JOB, make_render_handler(ffmpeg))
    yield m
    m.shutdown()


def run_job(manager, kind, payload, timeout=120):
    job = manager.submit(kind, kind, payload)
    assert manager.wait_idle(timeout)
    return manager.store.get(job.id)


def analyse(manager, clip) -> AudioLevels:
    job = run_job(manager, SMARTCUT_ANALYZE_JOB, {"path": str(clip), "media_id": "m", "duration": SECONDS,
                                                  "has_audio": True})
    assert job.state is JobState.COMPLETED, job.error
    return AudioLevels.from_dict(job.result["levels"])


def render_payload(clip, output, keep, **extra):
    return {"path": str(clip), "media_id": "m", "output": str(output), "duration": SECONDS, "fps": FPS,
            "has_audio": True, "audio_codec": "aac", "keep_ranges": [list(r) for r in keep] if keep else None,
            "output_settings": OutputSettings(encoder="cpu", quality="small").to_dict()} | extra


def onsets(values, rate, threshold):
    """Times where ``values`` rises above ``threshold`` after being below it."""
    above = values > threshold
    starts = np.nonzero(above[1:] & ~above[:-1])[0] + 1
    return [i / rate for i in starts]


def video_onsets(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=16:9,format=gray",
                          "-f", "rawvideo", "-"], check=True, capture_output=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, 16 * 9).mean(axis=1)
    return onsets(frames, FPS, 128)


def audio_onsets(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", "8000",
                          "-f", "s16le", "-"], check=True, capture_output=True).stdout
    samples = np.abs(np.frombuffer(raw, np.int16).astype(np.float32))
    envelope = np.convolve(samples, np.ones(40) / 40, mode="same")  # 5 ms smoothing
    return onsets(envelope, 8000, 3000)


def test_analysis_finds_the_silences(manager, clip):
    levels = analyse(manager, clip)
    assert abs(levels.duration - SECONDS) < 0.01 and abs(len(levels.peak) * levels.window - SECONDS) < 0.1
    state = SmartCutState(SmartCutSettings(), levels)
    state.detect()
    silences = [(s.start, s.end) for s in state.segments if s.kind == "silence"]
    # Silences 1-2, 3-4, 5-6, 7-8 s, shrunk by the padding (0.25 s after speech, 0.15 s before);
    # the last one reaches the end of the clip, where no padding is needed.
    expected = [(1.25, 1.85), (3.25, 3.85), (5.25, 5.85), (7.25, 8.0)]
    assert len(silences) == 4
    for (a, b), (ea, eb) in zip(silences, expected):
        assert abs(a - ea) < 0.06 and abs(b - eb) < 0.06, silences


def test_cut_export_keeps_audio_and_video_in_sync(manager, clip, tmp_path):
    levels = analyse(manager, clip)
    state = SmartCutState(SmartCutSettings(), levels)
    state.detect()
    keep = state.apply()
    output = tmp_path / "edited.mp4"
    job = run_job(manager, RENDER_JOB, render_payload(clip, output, keep))
    assert job.state is JobState.COMPLETED, job.error
    assert not list(tmp_path.glob("*.part*"))
    meta = probe_video(str(output), find_tool("ffprobe"))
    expected = sum(b - a for a, b in keep)
    assert abs(meta.duration - expected) < 0.1, (meta.duration, expected)
    assert meta.has_audio and meta.audio_codec == "aac"
    v, a = video_onsets(output), audio_onsets(output)
    assert len(v) == 3 and len(a) == 3, (v, a)  # the tone at 0 s has no onset; three more follow
    for tv, ta in zip(v, a):
        assert abs(tv - ta) < 0.045, (v, a)  # within about one frame, after every cut
    # Every removed second shortens the output: the 2nd tone starts at 2.0 - (1.85 - 1.25) s.
    assert abs(v[0] - (2.0 - 0.6)) < 0.08, v


def test_captions_follow_the_cuts(manager, clip, tmp_path):
    keep = [(0.0, 1.2), (2.0, 3.2), (4.0, 8.0)]
    captions = remap_to_cuts([CaptionSegment(4.2, 5.0, "after two cuts")], keep)
    assert (captions[0].start, captions[0].end) == (2.6, 3.4)
    ass = build_ass(captions, CaptionStyle(font_size=40), 160, 90)
    output = tmp_path / "captioned.mp4"
    job = run_job(manager, RENDER_JOB, render_payload(clip, output, keep, ass=ass))
    assert job.state is JobState.COMPLETED, job.error
    assert abs(probe_video(str(output), find_tool("ffprobe")).duration - 6.4) < 0.1


def test_no_cuts_copies_aac_and_other_codec(manager, clip, tmp_path):
    output = tmp_path / "h265.mp4"
    settings = OutputSettings(codec="h265", encoder="cpu", quality="small", fps="30").to_dict()
    job = run_job(manager, RENDER_JOB, render_payload(clip, output, None, output_settings=settings))
    if job.state is JobState.FAILED and "cannot be encoded" in (job.error or ""):
        pytest.skip("this FFmpeg build has no libx265")
    assert job.state is JobState.COMPLETED, job.error
    meta = probe_video(str(output), find_tool("ffprobe"))
    assert meta.video_codec == "hevc" and abs(meta.fps - 30) < 0.01 and abs(meta.duration - SECONDS) < 0.1


def test_existing_output_and_source_are_protected(manager, clip, tmp_path):
    existing = tmp_path / "exists.mp4"
    existing.write_bytes(b"keep me")
    job = run_job(manager, RENDER_JOB, render_payload(clip, existing, None))
    assert job.state is JobState.FAILED and "already exists" in job.error
    assert existing.read_bytes() == b"keep me"
    job = run_job(manager, RENDER_JOB, render_payload(clip, clip, None, overwrite=True))
    assert job.state is JobState.FAILED and "different from the source" in job.error


def test_two_jobs_never_write_the_same_file(manager, clip, tmp_path):
    output = tmp_path / "same.mp4"
    first = manager.submit(RENDER_JOB, "a", render_payload(clip, output, [(0.0, 8.0)]))
    second = manager.submit(RENDER_JOB, "b", render_payload(clip, output, [(0.0, 8.0)], overwrite=True))
    assert manager.wait_idle(120)
    states = {manager.store.get(first.id).state, manager.store.get(second.id).state}
    errors = [manager.store.get(j.id).error or "" for j in (first, second)]
    # Both may run at once (2 workers): one must refuse; if they ran one after the other, both succeed.
    assert JobState.COMPLETED in states
    if JobState.FAILED in states:
        assert any("already writing" in e for e in errors)
    assert output.is_file()


def test_cancel_leaves_no_partial_file(manager, clip, tmp_path):
    long_clip = tmp_path / "long.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=1280x720:r=25:d=60",
                    "-f", "lavfi", "-i", "sine=d=60", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    str(long_clip)], check=True)
    output = tmp_path / "cancelled.mp4"
    payload = render_payload(long_clip, output, [(0.0, 20.0), (30.0, 60.0)]) | {"duration": 60}
    payload["output_settings"] = OutputSettings(encoder="cpu", quality="high").to_dict()
    job = manager.submit(RENDER_JOB, "long", payload)
    deadline = time.monotonic() + 30
    while manager.store.get(job.id).state is not JobState.RUNNING and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    manager.cancel(job.id)
    assert manager.wait_idle(60)
    assert manager.store.get(job.id).state is JobState.CANCELLED
    assert not output.exists() and not list(tmp_path.glob("cancelled.part*"))


def test_ffmpeg_failure_is_reported(manager, tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video" * 100)
    job = run_job(manager, RENDER_JOB, render_payload(broken, tmp_path / "out.mp4", [(0.0, 1.0)]))
    assert job.state is JobState.FAILED and job.error.startswith("Export failed")
    assert not (tmp_path / "out.mp4").exists()
    analysis = run_job(manager, SMARTCUT_ANALYZE_JOB, {"path": str(broken), "media_id": "m", "duration": 1})
    assert analysis.state is JobState.FAILED and "Audio analysis failed" in analysis.error


def test_video_without_audio(manager, tmp_path, sample_videos):
    silent = sample_videos["vertical"]  # no audio track
    job = run_job(manager, SMARTCUT_ANALYZE_JOB, {"path": str(silent), "media_id": "m", "duration": 1})
    assert job.state is JobState.FAILED and "no audio track" in job.error
    output = tmp_path / "video only.mp4"
    payload = render_payload(silent, output, [(0.0, 0.4), (0.6, 1.0)]) | {"has_audio": False, "duration": 1.0}
    job = run_job(manager, RENDER_JOB, payload)
    assert job.state is JobState.COMPLETED, job.error
    meta = probe_video(str(output), find_tool("ffprobe"))
    assert not meta.has_audio and abs(meta.duration - 0.8) < 0.1


def test_threading_is_safe(manager, clip, tmp_path):
    """Several analyses at once share nothing."""
    results = []

    def worker():
        results.append(analyse(manager, clip).peak[:10])

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 2 and results[0] == results[1]

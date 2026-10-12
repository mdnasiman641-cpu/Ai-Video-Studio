"""Combined export against real FFmpeg: automatic Smart Cut + resize + captions + speed + audio
effects in one job and one encode, with audio/video sync checked on the output.

Uses the Smart Cut test clip (one second of tone with a white picture, one second of silence
with a black picture, repeated), so tone onsets and white-frame onsets must still coincide
after the cuts and the speed change.
"""

import json
import subprocess

import pytest

from ai_video_studio.captions.model import CaptionSegment
from ai_video_studio.captions.style import BUILTIN_PRESETS
from ai_video_studio.core.ffmpeg_tools import find_tool, probe_video
from ai_video_studio.jobs import render_jobs
from ai_video_studio.jobs.models import JobState
from ai_video_studio.jobs.render_jobs import RENDER_JOB
from ai_video_studio.render.effects import EditEffects
from ai_video_studio.render.layout import ResizeSettings, compute_layout
from ai_video_studio.smartcut.model import SmartCutSettings, SmartCutState
from tests.conftest import requires_ffmpeg
from tests.integration import test_smartcut_real as smartcut_tests
from tests.integration.test_smartcut_real import (
    FPS,
    SECONDS,
    analyse,
    audio_onsets,
    render_payload,
    run_job,
    video_onsets,
)

np = pytest.importorskip("numpy")
clip, manager = smartcut_tests.clip, smartcut_tests.manager  # the Smart Cut test clip and job manager fixtures
pytestmark = requires_ffmpeg


def stream_durations(path) -> dict[str, float]:
    out = subprocess.run([find_tool("ffprobe"), "-v", "error", "-show_entries", "stream=codec_type,duration",
                          "-of", "json", str(path)], check=True, capture_output=True, text=True).stdout
    return {s["codec_type"]: float(s["duration"]) for s in json.loads(out)["streams"]}


@pytest.fixture
def graphs(monkeypatch):
    """Record the filter graph of each export (the real command still runs)."""
    seen = []
    original = render_jobs.render_args

    def spy(ffmpeg, source, graph, *args, **kwargs):
        seen.append(graph)
        return original(ffmpeg, source, graph, *args, **kwargs)

    monkeypatch.setattr(render_jobs, "render_args", spy)
    return seen


def test_all_operations_in_one_export_keep_sync(manager, clip, tmp_path, graphs):
    levels = analyse(manager, clip)
    state = SmartCutState(SmartCutSettings(), levels)
    state.detect()
    expected_kept = sum(b - a for a, b in state.proposal_ranges())
    layout = compute_layout(160, 90, ResizeSettings(preset="custom", width=160, height=288, mode="blur"))
    effects = EditEffects(speed_enabled=True, video_speed=2.0, audio_enabled=True, volume=150, fade_in=0.1,
                          bass_db=3)
    captions = {"segments": [CaptionSegment(0.1, 0.9, "first").to_dict(), CaptionSegment(4.1, 4.9, "third").to_dict()],
                "style": BUILTIN_PRESETS["Bold Yellow"].to_dict(), "width": 160, "height": 288, "title": "t"}
    output = tmp_path / "combined.mp4"
    payload = render_payload(clip, output, None, stage={"type": "layout", "layout": layout.to_dict()},
                             smartcut_auto={"settings": SmartCutSettings().to_dict()}, captions=captions,
                             effects=effects.to_dict())
    job = run_job(manager, RENDER_JOB, payload)
    assert job.state is JobState.COMPLETED, job.error
    graph = graphs[-1]
    order = [graph.index(s) for s in ("concat=n=", "boxblur", "setpts=PTS/2", f"fps={FPS}", "subtitles=")]
    assert order == sorted(order)  # cuts → resize → speed → constant frame rate → captions
    assert "atempo=2" in graph and "volume=1.5" in graph and "bass=g=3" in graph and "afade=t=in" in graph
    assert any("Smart Cut removed" in w for w in job.result["warnings"])
    assert any("speed 2×" in w for w in job.result["warnings"])
    meta = probe_video(str(output), find_tool("ffprobe"))
    assert (meta.width, meta.height) == (160, 288) and meta.has_audio and abs(meta.fps - FPS) < 0.01
    assert abs(meta.duration - expected_kept / 2) < 0.12, (meta.duration, expected_kept)
    durations = stream_durations(output)
    assert abs(durations["audio"] - durations["video"]) < 0.08, durations
    v = video_onsets(output)
    a = [t for t in audio_onsets(output) if t > 0.2]  # the fade-in ramps the tone that starts at 0 s
    assert len(v) == 3 and len(a) == 3, (v, a)
    for tv, ta in zip(v, a):
        assert abs(tv - ta) < 0.06, (v, a)  # within about one output frame after cuts and 2× speed


def test_unlinked_audio_is_fitted_to_the_video_and_pitch_option(manager, clip, tmp_path):
    output = tmp_path / "unlinked.mp4"
    effects = EditEffects(speed_enabled=True, video_speed=2.0, link_audio=False, audio_speed=1.0)
    job = run_job(manager, RENDER_JOB, render_payload(clip, output, None, effects=effects.to_dict()))
    assert job.state is JobState.COMPLETED, job.error
    durations = stream_durations(output)
    assert abs(durations["video"] - SECONDS / 2) < 0.1 and abs(durations["audio"] - durations["video"]) < 0.08
    tape = tmp_path / "tape.mp4"
    effects = EditEffects(speed_enabled=True, video_speed=0.5, preserve_pitch=False)
    job = run_job(manager, RENDER_JOB, render_payload(clip, tape, [(0.0, 2.0)], effects=effects.to_dict()))
    assert job.state is JobState.COMPLETED, job.error
    durations = stream_durations(tape)
    assert abs(durations["video"] - 4.0) < 0.1 and abs(durations["audio"] - 4.0) < 0.1


def test_silent_video_with_speed_and_audio_effects(manager, tmp_path, sample_videos):
    silent = sample_videos["vertical"]  # 1 s, no audio track
    output = tmp_path / "slow.mp4"
    effects = EditEffects(speed_enabled=True, video_speed=0.5, audio_enabled=True, volume=200,
                          noise_reduction="light")
    payload = render_payload(silent, output, None, effects=effects.to_dict(), has_audio=False, duration=1.0,
                             smartcut_auto={"settings": SmartCutSettings().to_dict()})
    job = run_job(manager, RENDER_JOB, payload)
    assert job.state is JobState.COMPLETED, job.error
    meta = probe_video(str(output), find_tool("ffprobe"))
    assert not meta.has_audio and abs(meta.duration - 2.0) < 0.1
    warnings = " ".join(job.result["warnings"])
    assert "Smart Cut was skipped" in warnings and "Audio settings were not applied" in warnings


def test_invalid_effects_fail_clearly_and_leave_no_output(manager, clip, tmp_path):
    output = tmp_path / "bad.mp4"
    job = run_job(manager, RENDER_JOB, render_payload(clip, output, None,
                                                       effects={"speed_enabled": True, "video_speed": 9}))
    assert job.state is JobState.FAILED and "speed must be between" in job.error
    assert not output.exists() and not list(tmp_path.glob("*.part*"))

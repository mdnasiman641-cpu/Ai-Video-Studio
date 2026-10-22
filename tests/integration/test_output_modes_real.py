"""Export modes, encoder fallback, containers, the final check and the analysis cache with real
FFmpeg exports of tiny generated videos."""

import subprocess

import pytest

from ai_video_studio.core import ffmpeg_commands as fc
from ai_video_studio.core.ffmpeg_tools import find_tool, probe_video
from ai_video_studio.jobs.manager import JobManager
from ai_video_studio.jobs.models import JobState
from ai_video_studio.jobs.render_jobs import RENDER_JOB, make_render_handler
from ai_video_studio.jobs.store import JobStore
from ai_video_studio.reframe.model import ReframeSettings
from ai_video_studio.render.batch import BatchItem, RenderPreset, build_payload
from ai_video_studio.render.layout import ResizeSettings
from ai_video_studio.render.output import OutputSettings
from ai_video_studio.core.ffmpeg_tools import probe_video as probe
from tests.conftest import requires_ffmpeg

pytestmark = requires_ffmpeg


def ffmpeg():
    return find_tool("ffmpeg")


@pytest.fixture
def manager(db):
    m = JobManager(JobStore(db), 2)
    m.register(RENDER_JOB, make_render_handler(ffmpeg))
    yield m
    m.shutdown()


def run(manager, payload, timeout=120):
    job = manager.submit(RENDER_JOB, RENDER_JOB, payload)
    assert manager.wait_idle(timeout)
    return manager.store.get(job.id)


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    """4 s of moving test pattern with sound and 1 s pauses (for Smart Cut)."""
    path = tmp_path_factory.mktemp("modes") / "talk.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=4,noise=alls=10:allf=t",
                    "-f", "lavfi", "-i", "aevalsrc='0.4*sin(2*PI*330*t)*lt(mod(t,2),1.2)':s=48000:d=4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)
    return path


def payload(src, out, settings: OutputSettings, **extra):
    return {"path": str(src), "media_id": None, "output": str(out), "duration": 4.0, "fps": 25.0,
            "has_audio": True, "audio_codec": "aac", "keep_ranges": None,
            "output_settings": settings.to_dict()} | extra


def test_draft_and_maximum_really_differ(manager, clip, tmp_path):
    sizes = {}
    for mode in ("draft", "maximum"):
        out = tmp_path / f"{mode}.mp4"
        job = run(manager, payload(clip, out, OutputSettings(mode=mode, encoder="cpu")))
        assert job.state is JobState.COMPLETED, job.error
        assert job.result["encoder_label"] == "CPU (libx264)" and job.result["timings"]["render"] > 0
        assert job.result["mode"] in ("Draft / Fast Preview", "Maximum Quality")
        sizes[mode] = out.stat().st_size
    assert sizes["maximum"] > sizes["draft"] * 2  # CRF 16 / slower against CRF 30 / ultrafast


def test_gpu_encoder_choice_is_truthful_without_a_gpu(manager, clip, tmp_path):
    if fc.nvenc_usable(ffmpeg()):
        pytest.skip("NVENC works on this machine")
    strict = run(manager, payload(clip, tmp_path / "strict.mp4",
                                  OutputSettings(mode="fast", encoder="nvenc", allow_fallback=False)))
    assert strict.state is JobState.FAILED and "not usable" in strict.error and not (tmp_path / "strict.mp4").exists()
    loose = run(manager, payload(clip, tmp_path / "loose.mp4", OutputSettings(mode="fast", encoder="nvenc")))
    assert loose.state is JobState.COMPLETED and loose.result["encoder"] == "libx264"
    assert loose.result["encoder_label"] == "CPU (libx264)"  # never labelled as GPU
    assert any("NVIDIA NVENC" in w and "using CPU" in w for w in loose.result["warnings"])


def test_mkv_container_and_hardware_decoding_with_cpu_fallback(manager, clip, tmp_path):
    out = tmp_path / "out.mkv"
    job = run(manager, payload(clip, out, OutputSettings(mode="fast", encoder="cpu", container="mkv",
                                                          hw_decode="auto")))
    assert job.state is JobState.COMPLETED, job.error
    meta = probe_video(out, find_tool("ffprobe"))
    assert meta.format_name and "matroska" in meta.format_name and meta.has_audio and meta.width == 640


def test_final_check_rejects_a_wrong_size(manager, clip, tmp_path):
    out = tmp_path / "wrong.mp4"
    job = run(manager, payload(clip, out, OutputSettings(mode="draft", encoder="cpu"),
                               expect={"width": 1080, "height": 1920, "fps": None}))
    assert job.state is JobState.FAILED and "640×360 instead of 1080×1920" in job.error and not out.exists()


def test_smart_cut_analysis_is_reused_from_the_cache(manager, clip, tmp_path):
    p = {"smartcut_auto": {"settings": {"threshold_db": -35, "min_silence": 0.4}}}
    first = run(manager, payload(clip, tmp_path / "a.mp4", OutputSettings(mode="draft", encoder="cpu"), **p))
    second = run(manager, payload(clip, tmp_path / "b.mp4", OutputSettings(mode="draft", encoder="cpu"), **p))
    assert first.state is second.state is JobState.COMPLETED, (first.error, second.error)
    assert not any("reused" in w for w in first.result["warnings"])
    assert any("reused the audio levels" in w for w in second.result["warnings"])
    d1 = probe_video(tmp_path / "a.mp4", find_tool("ffprobe")).duration
    d2 = probe_video(tmp_path / "b.mp4", find_tool("ffprobe")).duration
    assert abs(d1 - d2) < 0.05 and d1 < 3.5  # the same cuts


def test_custom_size_and_custom_aspect_reframe_from_a_batch_preset(manager, clip, tmp_path):
    item = BatchItem(str(clip), metadata=probe(clip, find_tool("ffprobe")))
    preset = RenderPreset(operation="reframe", tracking_mode="center", output=OutputSettings(mode="draft",
                          encoder="cpu"), resize=ResizeSettings(preset="custom", width=500, height=250))
    out = tmp_path / "wide.mp4"
    p, _notes = build_payload(item, preset, out, overwrite=False, batch_id="b", captions=None, caption_style=None,
                              tracking_defaults=ReframeSettings(), models_dir=str(tmp_path))
    assert p["reframe_auto"]["settings"]["preset"] == "custom" and p["expect"]["width"] == 500
    job = run(manager, p)
    assert job.state is JobState.COMPLETED, job.error
    meta = probe_video(out, find_tool("ffprobe"))
    assert (meta.width, meta.height) == (500, 250)

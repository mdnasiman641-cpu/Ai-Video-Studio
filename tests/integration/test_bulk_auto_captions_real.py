"""Bulk Processing automatic captions: each batch export job transcribes its own video with the
Captions page's Whisper transcription (faked here, no model download), then times, styles and
burns the captions in the same single export as Smart Cut and speed. Real FFmpeg."""

import subprocess
from types import SimpleNamespace

import pytest

from ai_video_studio.captions.style import BUILTIN_PRESETS
from ai_video_studio.core.ffmpeg_tools import VideoMetadata
from ai_video_studio.jobs import render_jobs
from ai_video_studio.jobs.manager import JobManager
from ai_video_studio.jobs.models import JobState
from ai_video_studio.jobs.render_jobs import RENDER_JOB, make_render_handler
from ai_video_studio.jobs.store import JobStore
from ai_video_studio.reframe.model import ReframeSettings
from ai_video_studio.render.batch import BatchItem, RenderPreset, build_payload
from ai_video_studio.render.effects import EditEffects
from ai_video_studio.render.layout import ResizeSettings
from ai_video_studio.render.output import OutputSettings
from ai_video_studio.smartcut.model import SmartCutSettings, SmartCutState
from tests.conftest import requires_ffmpeg
from tests.integration import test_smartcut_real as smartcut_tests
from tests.integration.test_smartcut_real import FPS, SECONDS, analyse

pytestmark = requires_ffmpeg
clip = smartcut_tests.clip  # 8 s: tone + white picture during the first second of every two
TRANSCRIPTION = {"model": "small", "language": "en", "device": "cpu", "model_path": None, "models_dir": None,
                 "max_chars": 42, "max_seconds": 6}


class PerVideoWhisper:
    """A fake faster-whisper model whose captions depend on the audio it is given (its length),
    so each video's job must transcribe that video's own audio."""

    def __init__(self, fail_longer_than=None):
        self.calls = []
        self.fail_longer_than = fail_longer_than

    def __call__(self, name, device, compute_type, download_root):
        return self

    def transcribe(self, audio, **kwargs):
        seconds = round(len(audio) / 16000)
        self.calls.append(seconds)
        if self.fail_longer_than is not None and seconds > self.fail_longer_than:
            raise RuntimeError("decoder crashed")
        segments = []
        for start in range(0, seconds, 2):  # one caption per tone (tones start at even seconds)
            word = SimpleNamespace(start=start + 0.2, end=start + 0.8, word=f" tone{start}of{seconds}")
            segments.append(SimpleNamespace(start=word.start, end=word.end, text=word.word, words=[word]))
        info = SimpleNamespace(language="en", language_probability=0.99, duration=seconds)
        return iter(segments), info


def make_clip(path, seconds):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s=160x90:r={FPS}:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:v", "libx264", "-pix_fmt",
                    "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def run_all(manager, payloads, timeout=180):
    jobs = [manager.submit(RENDER_JOB, p["output"], p) for p in payloads]
    assert manager.wait_idle(timeout)
    return [manager.store.get(j.id) for j in jobs]


@pytest.fixture
def spy(monkeypatch):
    """Record the captions and style each export burns in (the real ASS is still rendered)."""
    seen = []
    original = render_jobs.build_ass

    def build(segments, style, width, height, title=""):
        seen.append({"title": title, "segments": [(s.start, s.end, s.text) for s in segments], "style": style})
        return original(segments, style, width, height, title=title)

    monkeypatch.setattr(render_jobs, "build_ass", build)
    return seen


def batch_payload(path, output, meta, preset):
    item = BatchItem(str(path), metadata=meta)
    payload, notes = build_payload(item, preset, output, overwrite=False, batch_id="b", captions=None,
                                   caption_style=None, tracking_defaults=ReframeSettings(), models_dir="/m",
                                   transcription=TRANSCRIPTION)
    return payload | {"output_settings": OutputSettings(encoder="cpu", quality="small").to_dict()}


def auto_preset(**kw) -> RenderPreset:
    return RenderPreset(operation="none", resize=ResizeSettings(burn_captions=True), auto_captions=True,
                        caption_style=BUILTIN_PRESETS["Bold Green"], **kw)


def test_each_video_gets_its_own_captions_timed_after_smart_cut_and_speed(db, clip, tmp_path, spy):
    whisper = PerVideoWhisper()
    manager = JobManager(JobStore(db), 1)
    manager.register(RENDER_JOB, make_render_handler(smartcut_tests.ffmpeg, model_factory=whisper, cuda_devices=0))
    manager.register(smartcut_tests.SMARTCUT_ANALYZE_JOB, smartcut_tests.make_smartcut_analyze_handler(smartcut_tests.ffmpeg))
    other = make_clip(tmp_path / "other.mp4", 4)
    meta8 = VideoMetadata(SECONDS, 160, 90, FPS, "h264", has_audio=True, audio_codec="aac")
    meta4 = VideoMetadata(4.0, 160, 90, FPS, "h264", has_audio=True, audio_codec="aac")
    edited = auto_preset(smart_cut=True, effects=EditEffects(speed_enabled=True, video_speed=2.0))
    payloads = [batch_payload(clip, tmp_path / "a.mp4", meta8, edited),
                batch_payload(other, tmp_path / "b.mp4", meta4, auto_preset())]
    assert payloads[0]["captions_auto"]["model"] == "small" and payloads[0]["captions"]["segments"] == []
    jobs = run_all(manager, payloads)
    assert [j.state for j in jobs] == [JobState.COMPLETED, JobState.COMPLETED], [j.error for j in jobs]
    assert sorted(whisper.calls) == [4, 8]  # the existing transcription ran once per video, on its own audio
    by_title = {s["title"]: s for s in spy}
    a, b = by_title["tone gaps.mp4"], by_title["other.mp4"]
    assert all("of8" in text for *_t, text in a["segments"]) and all("of4" in text for *_t, text in b["segments"])
    assert a["style"] == BUILTIN_PRESETS["Bold Green"] == b["style"]  # the chosen style
    # timing: Smart Cut (the same detection the job ran), then 2× speed, applied exactly once
    levels = analyse(manager, clip)
    state = SmartCutState(SmartCutSettings(), levels)
    state.detect()
    ranges = state.proposal_ranges()
    offsets, total = [], 0.0
    for start, end in ranges:
        offsets.append(total)
        total += end - start
    expected = []
    for tone in range(0, SECONDS, 2):
        for (start, end), offset in zip(ranges, offsets):
            if start <= tone + 0.2 <= end:
                expected.append(((offset + tone + 0.2 - start) / 2, (offset + tone + 0.8 - start) / 2))
    assert len(a["segments"]) == 4
    for (s, e, _text), (es, ee) in zip(a["segments"], expected):
        assert abs(s - es) < 0.05 and abs(e - ee) < 0.05, (a["segments"], expected)
    assert [(s, e) for s, e, _t in b["segments"]] == [(0.2, 0.8), (2.2, 2.8)]  # no cuts, no speed: unchanged
    assert any("caption(s) generated" in w for w in jobs[0].result["warnings"])
    manager.shutdown()


def test_caption_failure_fails_only_that_video_and_retry_works(db, clip, tmp_path, spy):
    whisper = PerVideoWhisper(fail_longer_than=5)  # the 8 s video fails
    manager = JobManager(JobStore(db), 1)
    manager.register(RENDER_JOB, make_render_handler(smartcut_tests.ffmpeg, model_factory=whisper, cuda_devices=0))
    short = make_clip(tmp_path / "short.mp4", 4)
    meta8 = VideoMetadata(SECONDS, 160, 90, FPS, "h264", has_audio=True, audio_codec="aac")
    meta4 = VideoMetadata(4.0, 160, 90, FPS, "h264", has_audio=True, audio_codec="aac")
    failing, ok = run_all(manager, [batch_payload(clip, tmp_path / "a.mp4", meta8, auto_preset()),
                                    batch_payload(short, tmp_path / "b.mp4", meta4, auto_preset())])
    assert failing.state is JobState.FAILED and "Automatic captions failed" in failing.error
    assert "decoder crashed" in failing.error and not (tmp_path / "a.mp4").exists()  # never without captions
    assert ok.state is JobState.COMPLETED and (tmp_path / "b.mp4").is_file()
    whisper.fail_longer_than = None
    manager.retry(failing.id)
    assert manager.wait_idle(120) and manager.store.get(failing.id).state is JobState.COMPLETED
    manager.shutdown()


def test_missing_model_and_silent_video_fail_with_clear_reasons(db, tmp_path, sample_videos):
    manager = JobManager(JobStore(db), 1)
    manager.register(RENDER_JOB, make_render_handler(smartcut_tests.ffmpeg))  # the real model lookup; nothing installed here
    talking = make_clip(tmp_path / "talk.mp4", 2)
    meta = VideoMetadata(2.0, 160, 90, FPS, "h264", has_audio=True, audio_codec="aac")
    silent_meta = VideoMetadata(1.0, 320, 180, 25, "h264", has_audio=False)
    missing, silent = run_all(manager, [
        batch_payload(talking, tmp_path / "a.mp4", meta, auto_preset()),
        batch_payload(sample_videos["vertical"], tmp_path / "s.mp4", silent_meta, auto_preset())])
    assert missing.state is JobState.FAILED and "Whisper model 'small'" in missing.error
    assert "Settings → Speech recognition models" in missing.error and "never download" in missing.error
    assert silent.state is JobState.FAILED and "no audio track" in silent.error
    assert not list((tmp_path).glob("*.mp4.part*")) and not (tmp_path / "a.mp4").exists()
    manager.shutdown()

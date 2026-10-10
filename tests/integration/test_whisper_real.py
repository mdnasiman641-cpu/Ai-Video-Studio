"""Real faster-whisper transcription. No fake models are used in this file.

Runs only when ``AVS_TEST_WHISPER_MODEL_DIR`` points to a downloaded faster-whisper
(CTranslate2) model folder, e.g. ``Systran/faster-whisper-tiny``. CI downloads it; locally:

    python -c "from huggingface_hub import snapshot_download as d; d('Systran/faster-whisper-tiny', local_dir='.cache/whisper-tiny')"
    set AVS_TEST_WHISPER_MODEL_DIR=.cache\\whisper-tiny      (Windows)

The audio fixtures in tests/fixtures are synthetic espeak-ng speech (see README there).
Transcripts are printed so CI logs show what the model actually produced.
"""

import os
import subprocess
from pathlib import Path

import pytest

from ai_video_studio.captions.transcriber import (
    TranscriptionOptions,
    clear_model_cache,
    cuda_device_count,
    transcribe_file,
)
from ai_video_studio.core.ffmpeg_tools import find_tool
from ai_video_studio.jobs.caption_jobs import TRANSCRIBE_JOB, make_transcribe_handler
from ai_video_studio.jobs.manager import JobManager
from ai_video_studio.jobs.models import JobState
from ai_video_studio.jobs.store import JobStore
from tests.conftest import requires_ffmpeg

MODEL_DIR = os.environ.get("AVS_TEST_WHISPER_MODEL_DIR")
BN_MODEL_DIR = os.environ.get("AVS_TEST_WHISPER_BN_MODEL_DIR") or None
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
EN_AUDIO = FIXTURES / "speech_en.flac"
BN_AUDIO = FIXTURES / "speech_bn.flac"
AUDIO_SECONDS = 6.2

MODEL_AVAILABLE = bool(MODEL_DIR) and Path(MODEL_DIR).is_dir()

if not MODEL_AVAILABLE and os.environ.get("AVS_REQUIRE_REAL_WHISPER") == "1":
    # CI sets this so a failed model download can never turn into silently skipped tests.
    raise RuntimeError(f"AVS_REQUIRE_REAL_WHISPER=1 but no model folder at {MODEL_DIR!r}")

pytestmark = [requires_ffmpeg, pytest.mark.skipif(
    not MODEL_AVAILABLE,
    reason="AVS_TEST_WHISPER_MODEL_DIR not set to a downloaded faster-whisper model (real transcription not tested)",
)]


@pytest.fixture(scope="module")
def wavs(tmp_path_factory):
    """Convert the FLAC fixtures to the 16 kHz mono WAV that the app's extraction step produces."""
    folder = tmp_path_factory.mktemp("wav")
    out = {}
    for lang, src in (("en", EN_AUDIO), ("bn", BN_AUDIO)):
        dst = folder / f"{lang}.wav"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ac", "1", "-ar", "16000",
                        "-c:a", "pcm_s16le", str(dst)], check=True)
        out[lang] = str(dst)
    return out


@pytest.fixture(autouse=True)
def fresh_engine():
    clear_model_cache()
    yield
    clear_model_cache()


def _text(result) -> str:
    return " ".join(s.text for s in result.track.segments)


def _check_timing(result, max_end=AUDIO_SECONDS):
    segs = result.track.segments
    assert segs, "the real model returned no captions"
    for a, b in zip(segs, segs[1:]):
        assert a.start <= b.start and a.end <= b.start + 1e-3
    for s in segs:
        assert 0 <= s.start < s.end <= max_end + 0.5


def test_english_transcription_cpu(wavs):
    result = transcribe_file(wavs["en"], TranscriptionOptions(model_path=MODEL_DIR, language="en", device="cpu"))
    text = _text(result).lower()
    print(f"\n[real whisper/en] device={result.device} text={text!r}")
    assert result.device == "cpu" and result.language == "en"
    hits = [w for w in ("hello", "welcome", "video", "studio", "testing", "caption") if w in text]
    assert len(hits) >= 3, f"too few expected words recognised: {hits}"
    _check_timing(result)
    assert all(seg.words for seg in result.track.segments), "word timestamps missing"


def test_bengali_transcription_cpu(wavs):
    # VAD is turned off here: the robotic synthetic Bengali voice is easily classified as
    # non-speech, and this test checks the Bengali language path, not voice detection.
    # Uses AVS_TEST_WHISPER_BN_MODEL_DIR (CI: "small", the app default) when set.
    model_dir = BN_MODEL_DIR or MODEL_DIR
    options = TranscriptionOptions(model_path=model_dir, language="bn", device="cpu", vad_filter=False)
    result = transcribe_file(wavs["bn"], options)
    text = _text(result)
    detail = f"model={Path(model_dir).name} segments={len(result.track.segments)} text={text!r}"
    print(f"\n[real whisper/bn] device={result.device} {detail}")
    assert result.language == "bn"
    assert len(text.strip()) >= 5, f"no speech recognised; {detail}"
    bengali_chars = sum(1 for ch in text if "ঀ" <= ch <= "৿")
    if bengali_chars < 5 and "tiny" in Path(model_dir).name:
        # Observed in CI: tiny recognises the audio but writes romanised Latin text
        # ("Ami magai kota mori ..."). Recorded as a known model limitation, not hidden.
        pytest.xfail(f"whisper-tiny writes Bengali in Latin script; use small or larger; {detail}")
    # Only the script is asserted: accuracy on synthetic speech is low, so quality must be
    # judged by a person on real recordings.
    assert bengali_chars >= 5, f"expected Bengali script output when language=bn; {detail}"
    _check_timing(result)


def test_auto_language_detects_english(wavs):
    result = transcribe_file(wavs["en"], TranscriptionOptions(model_path=MODEL_DIR, language="auto", device="cpu"))
    print(f"\n[real whisper/auto] detected={result.language} p={result.language_probability:.2f}")
    assert result.language == "en"


def test_auto_device_matches_hardware(wavs):
    result = transcribe_file(wavs["en"], TranscriptionOptions(model_path=MODEL_DIR, language="en", device="auto"))
    gpus = cuda_device_count()
    print(f"\n[real whisper/auto-device] cuda_devices={gpus} used={result.device} warnings={result.warnings}")
    if gpus == 0:
        assert result.device == "cpu" and not result.warnings
    else:
        # A visible GPU is used if the CUDA libraries load, otherwise the fallback is reported.
        assert result.device == "cuda" or any("falling back" in w or "retried" in w for w in result.warnings)


def test_forced_cuda_without_gpu_falls_back_to_cpu(wavs):
    if cuda_device_count() > 0:
        pytest.skip("a CUDA device is present; fallback-without-GPU cannot be exercised here")
    result = transcribe_file(wavs["en"], TranscriptionOptions(model_path=MODEL_DIR, language="en", device="cuda"))
    print(f"\n[real whisper/forced-cuda] used={result.device} warnings={result.warnings}")
    assert result.device == "cpu"
    assert any("CPU" in w for w in result.warnings)
    assert "welcome" in _text(result).lower() or "hello" in _text(result).lower()


@requires_ffmpeg
def test_full_transcription_job_from_video(db, tmp_path):
    """Video → FFmpeg audio extraction → real Whisper → caption track, through the job queue."""
    video = tmp_path / "speech video.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x180:r=25",
                    "-i", str(EN_AUDIO), "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(video)], check=True)
    manager = JobManager(JobStore(db), 1)
    manager.register(TRANSCRIBE_JOB, make_transcribe_handler(lambda: find_tool("ffmpeg")))
    job = manager.submit(TRANSCRIBE_JOB, "real", {"path": str(video), "media_id": "m", "language": "en",
                                                  "device": "cpu", "model_path": MODEL_DIR, "duration": 6.2})
    assert manager.wait_idle(300)
    job = manager.store.get(job.id)
    manager.shutdown()
    assert job.state is JobState.COMPLETED, job.error
    texts = " ".join(s["text"] for s in job.result["track"]["segments"]).lower()
    print(f"\n[real whisper/job] {texts!r}")
    assert job.result["device"] == "cpu"
    assert "welcome" in texts or "hello" in texts

"""Real faster-whisper transcription. Opt-in: needs a downloaded CTranslate2 model.

Set AVS_TEST_WHISPER_MODEL_DIR to a local faster-whisper model folder (e.g. a copy of
Systran/faster-whisper-tiny) to run it. Speech is synthesised with espeak-ng.
"""

import os
import shutil
import subprocess

import pytest

from ai_video_studio.captions.transcriber import TranscriptionOptions, clear_model_cache, transcribe_file

MODEL_DIR = os.environ.get("AVS_TEST_WHISPER_MODEL_DIR")

pytestmark = [
    pytest.mark.skipif(not MODEL_DIR, reason="AVS_TEST_WHISPER_MODEL_DIR not set (no local Whisper model)"),
    pytest.mark.skipif(shutil.which("espeak-ng") is None, reason="espeak-ng not installed"),
]


def test_transcribes_synthesised_english(tmp_path):
    wav = tmp_path / "speech.wav"
    subprocess.run(["espeak-ng", "-v", "en-us", "-s", "140", "-w", str(wav),
                    "Hello and welcome. Today we are testing automatic captions."], check=True)
    clear_model_cache()
    result = transcribe_file(str(wav), TranscriptionOptions(model_path=MODEL_DIR, language="en", device="cpu"))
    text = " ".join(s.text for s in result.track.segments).lower()
    assert "welcome" in text and "caption" in text
    assert result.track.segments[0].start < 1.5

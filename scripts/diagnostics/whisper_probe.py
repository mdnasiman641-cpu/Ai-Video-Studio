"""Diagnostic: how real faster-whisper models handle the test speech fixtures.

Prints one line per (fixture, model, VAD) combination: detected language and probability,
segment count, Bengali-script character count, and the transcript (ASCII-escaped).
Used by .github/workflows/whisper-diagnostics.yml (manual trigger). Not a pass/fail test.

    python scripts/diagnostics/whisper_probe.py MODEL_DIR [MODEL_DIR ...]
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ai_video_studio.captions.transcriber import (  # noqa: E402
    TranscriptionOptions,
    clear_model_cache,
    transcribe_file,
)

FIXTURES = {"en": ROOT / "tests/fixtures/speech_en.flac", "bn": ROOT / "tests/fixtures/speech_bn.flac"}


def to_wav(src: Path, folder: Path) -> str:
    dst = folder / (src.stem + ".wav")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", str(dst)], check=True)
    return str(dst)


def main(model_dirs: list[str]) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        wavs = {k: to_wav(v, Path(tmp)) for k, v in FIXTURES.items()}
        for model_dir in model_dirs:
            name = Path(model_dir).name
            for fixture, wav in wavs.items():
                runs = [("auto", True), (fixture, True), (fixture, False)]
                for language, vad in runs:
                    clear_model_cache()
                    started = time.monotonic()
                    result = transcribe_file(wav, TranscriptionOptions(
                        model_path=model_dir, language=language, device="cpu", vad_filter=vad))
                    text = " ".join(s.text for s in result.track.segments)
                    bengali = sum(1 for ch in text if "ঀ" <= ch <= "৿")
                    print(f"[probe] model={name} fixture={fixture} language_arg={language} vad={vad} "
                          f"detected={result.language} p={result.language_probability:.2f} "
                          f"segments={len(result.track.segments)} bengali_chars={bengali} "
                          f"seconds={time.monotonic() - started:.1f} text={ascii(text)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

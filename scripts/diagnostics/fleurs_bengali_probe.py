"""Diagnostic: real human Bengali speech (Google FLEURS, bn_in test split, CC-BY-4.0).

Fetches a few utterances with reference transcripts through the Hugging Face
datasets-server API at run time (nothing is committed), transcribes them with the given
faster-whisper models on CPU (language=bn, the app's default VAD setting) and prints the
character error rate (CER) per utterance and overall. Not a pass/fail test.

    python scripts/diagnostics/fleurs_bengali_probe.py COUNT MODEL_DIR [MODEL_DIR ...]
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ai_video_studio.captions.transcriber import (  # noqa: E402
    TranscriptionOptions,
    clear_model_cache,
    transcribe_file,
)

API = "https://datasets-server.huggingface.co/rows"


def fetch_rows(count: int) -> list[dict]:
    query = urllib.parse.urlencode({"dataset": "google/fleurs", "config": "bn_in", "split": "test",
                                    "offset": 0, "length": count})
    with urllib.request.urlopen(f"{API}?{query}", timeout=60) as resp:
        data = json.load(resp)
    rows = []
    for item in data["rows"]:
        row = item["row"]
        audio = row["audio"][0]["src"] if isinstance(row["audio"], list) else row["audio"]["src"]
        rows.append({"id": row.get("id"), "audio": audio,
                     "text": row.get("transcription") or row.get("raw_transcription")})
    return rows


def normalise(text: str) -> str:
    """Keep letters/marks/digits only, so CER ignores punctuation and spacing differences."""
    text = unicodedata.normalize("NFC", text.lower())
    return "".join(ch for ch in text if unicodedata.category(ch)[0] in "LMN")


def cer(ref: str, hyp: str) -> float:
    r, h = normalise(ref), normalise(hyp)
    if not r:
        return 0.0 if not h else 1.0
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i]
        for j, hc in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc)))
        prev = cur
    return prev[-1] / len(r)


def main(count: int, model_dirs: list[str]) -> int:
    try:
        rows = fetch_rows(count)
    except Exception as exc:  # noqa: BLE001 - diagnostic: report and stop
        print(f"[fleurs] could not fetch FLEURS rows: {exc}")
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        wavs = []
        for n, row in enumerate(rows):
            raw = Path(tmp) / f"{n}.audio"
            urllib.request.urlretrieve(row["audio"], raw)
            wav = Path(tmp) / f"{n}.wav"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-ac", "1", "-ar", "16000",
                            "-c:a", "pcm_s16le", str(wav)], check=True)
            wavs.append(str(wav))
        print(f"[fleurs] fetched {len(wavs)} real Bengali utterances")
        for model_dir in model_dirs:
            name = Path(model_dir).name
            total_err = total_len = 0.0
            for row, wav in zip(rows, wavs):
                clear_model_cache()
                started = time.monotonic()
                result = transcribe_file(wav, TranscriptionOptions(model_path=model_dir, language="bn",
                                                                   device="cpu"))
                hyp = " ".join(s.text for s in result.track.segments)
                score = cer(row["text"], hyp)
                bengali = sum(1 for ch in hyp if "ঀ" <= ch <= "৿")
                total_err += score * len(normalise(row["text"]))
                total_len += len(normalise(row["text"]))
                print(f"[fleurs] model={name} id={row['id']} cer={score:.2f} bengali_chars={bengali} "
                      f"seconds={time.monotonic() - started:.1f} ref={ascii(row['text'])} hyp={ascii(hyp)}",
                      flush=True)
            print(f"[fleurs] model={name} OVERALL_CER={total_err / max(total_len, 1):.3f} over {len(wavs)} utterances")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]), sys.argv[2:]))

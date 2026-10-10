"""Diagnostic: real human Bengali speech (Google FLEURS, bn_in test split, CC-BY-4.0).

Downloads the bn_in test transcripts and audio from the dataset repository at run time
(nothing is committed), takes the first few utterances, transcribes them with the given
faster-whisper models on CPU (language=bn, the app's default VAD setting) and prints the
character error rate (CER) per utterance and overall. Not a pass/fail test.

    python scripts/diagnostics/fleurs_bengali_probe.py COUNT MODEL_DIR [MODEL_DIR ...]
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ai_video_studio.captions.transcriber import (  # noqa: E402
    TranscriptionOptions,
    clear_model_cache,
    transcribe_file,
)

REPO = "google/fleurs"


def fetch_rows(count: int, folder: Path) -> list[dict]:
    """Download the bn_in test transcripts and audio archive from the dataset repository and
    extract the first ``count`` utterances (the datasets-server rows API returns HTTP 500 for
    this script-based dataset)."""
    import tarfile

    from huggingface_hub import hf_hub_download

    tsv = hf_hub_download(REPO, "data/bn_in/test.tsv", repo_type="dataset")
    rows = []
    with open(tsv, encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 4:
                rows.append({"id": parts[0], "file": parts[1], "text": parts[3] or parts[2]})
            if len(rows) >= count:
                break
    wanted = {r["file"]: r for r in rows}
    archive = hf_hub_download(REPO, "data/bn_in/audio/test.tar.gz", repo_type="dataset")
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            name = Path(member.name).name
            if name in wanted and member.isfile():
                target = folder / name
                with tar.extractfile(member) as src, open(target, "wb") as dst:
                    dst.write(src.read())
                wanted[name]["audio"] = str(target)
                if all("audio" in r for r in rows):
                    break
    return [r for r in rows if "audio" in r]


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
    with tempfile.TemporaryDirectory() as tmp:
        try:
            rows = fetch_rows(count, Path(tmp))
        except Exception as exc:  # noqa: BLE001 - diagnostic: report and stop
            print(f"[fleurs] could not fetch FLEURS data: {exc}")
            return 1
        wavs = []
        for n, row in enumerate(rows):
            wav = Path(tmp) / f"{n}.wav"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", row["audio"], "-ac", "1", "-ar", "16000",
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

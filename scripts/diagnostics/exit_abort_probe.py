"""Diagnostic: which library combination aborts the process at exit on Windows.

Each mode runs in a fresh child process many times; the parent counts non-zero exits
and "Fatal Python error" messages. Not a pass/fail test.

    python scripts/diagnostics/exit_abort_probe.py REPEATS MODEL_DIR
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODES = ["qt", "av", "ct2", "qt_av", "qt_ct2", "qt_whisper", "whisper"]

CHILD = r'''
import os, sys, time
sys.path.insert(0, ROOT)
mode, video, model = sys.argv[1], sys.argv[2], sys.argv[3]
if "qt" in mode:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QUrl, QTimer, QEventLoop
    from PySide6.QtWidgets import QApplication
    from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
    app = QApplication([])
    player = QMediaPlayer(); sink = QVideoSink(); player.setVideoSink(sink)
    player.setSource(QUrl.fromLocalFile(video)); player.play()
    loop = QEventLoop(); QTimer.singleShot(700, loop.quit); loop.exec()
    player.stop()
if mode in ("av", "qt_av"):
    import av
    with av.open(video) as c:
        for i, f in enumerate(c.decode(audio=0)):
            if i > 20: break
if mode in ("ct2", "qt_ct2"):
    import ctranslate2, numpy as np
    m = ctranslate2.models.Whisper(model, device="cpu", compute_type="int8")
    feats = ctranslate2.StorageView.from_array(np.zeros((1, 80, 3000), dtype=np.float32))
    m.detect_language(feats)
if mode in ("whisper", "qt_whisper"):
    from ai_video_studio.captions.transcriber import TranscriptionOptions, transcribe_file, clear_model_cache
    wav = video + ".wav"
    import subprocess
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", video, "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav], check=True)
    transcribe_file(wav, TranscriptionOptions(model_path=model, language="en", device="cpu"))
    import av  # faster-whisper imports it anyway; make it explicit
print("child-done", mode, flush=True)
'''


def main(repeats: int, model_dir: str) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        video = str(Path(tmp) / "clip.mp4")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=2",
                        "-i", str(ROOT / "tests/fixtures/speech_en.flac"), "-shortest", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", video], check=True)
        script = Path(tmp) / "child.py"
        script.write_text(f"ROOT = {str(ROOT)!r}\n" + CHILD, encoding="utf-8")
        for mode in MODES:
            bad = fatal = 0
            for _ in range(repeats):
                proc = subprocess.run([sys.executable, str(script), mode, video, model_dir],
                                      capture_output=True, text=True, encoding="utf-8", errors="replace",
                                      env=dict(os.environ, PYTHONFAULTHANDLER="1"))
                if proc.returncode != 0 or "child-done" not in proc.stdout:
                    bad += 1
                if "Fatal Python error" in proc.stderr:
                    fatal += 1
                    last = proc.stderr.strip().splitlines()[:3]
            print(f"[exit-probe] mode={mode} runs={repeats} nonzero_exit={bad} fatal_errors={fatal}"
                  + (f" sample={last}" if fatal else ""), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]), sys.argv[2]))

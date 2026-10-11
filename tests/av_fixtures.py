"""Synthetic audio-visual clips for active-speaker tests (generated at test time, never committed).

Two copies of a real face photo (the public-domain NASA portrait from scikit-image) sit side by
side. The audio is real recorded speech (``tests/fixtures/speech_en.flac``, a synthetic voice)
arranged on a timeline. For each speech interval one face is the "speaker": a dark mouth opening
is drawn on its mouth, opened in proportion to the loudness of the audio at that moment, while
the other face's mouth stays closed. This gives real video for the face detector and real
speech for the voice-activity model, with a known ground truth. It is a controlled synthetic
test, not validation on real conversations.
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SPEECH = FIXTURES / "speech_en.flac"
W, H, FPS = 1280, 720, 25
FACE = 360
POSITIONS = {"A": (120, 180), "B": (800, 180)}


def _speech_samples(tmp: Path):
    import numpy as np

    wav = tmp / "speech16k.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(SPEECH), "-ac", "1", "-ar", "16000", str(wav)], check=True)
    with wave.open(str(wav), "rb") as fh:
        return np.frombuffer(fh.readframes(fh.getnframes()), dtype="<i2").astype(np.float32) / 32768.0


def build_av_clip(path: Path, image: Path, timeline: list[tuple[float, str | None]], duration: float,
                  extra_audio: str | None = None, visible: tuple[str, ...] = ("A", "B")) -> Path:
    """``timeline``: [(start_seconds, speaker)] where speaker is "A", "B" or None (off-screen
    voice: speech is heard but no visible mouth moves). Each entry plays the whole speech clip.
    ``extra_audio``: an FFmpeg lavfi source mixed in (e.g. music-like tones)."""
    import cv2
    import numpy as np

    tmp = path.parent
    speech = _speech_samples(tmp)
    sr = 16000
    audio = np.zeros(int(duration * sr), np.float32)
    owner = np.full(len(audio), "", dtype=object)
    for start, who in timeline:
        a = int(start * sr)
        b = min(len(audio), a + len(speech))
        audio[a:b] += speech[: b - a]
        owner[a:b] = who or "off"
    face = cv2.resize(cv2.imread(str(image)), (FACE, FACE))
    mirrored = cv2.flip(face, 1)
    # Mouth position on the photo: YuNet landmarks would also work; fixed coordinates keep the
    # fixture independent of the detector under test (measured on the scaled photo).
    mouth = {"A": (POSITIONS["A"][0] + 160, POSITIONS["A"][1] + 128),
             "B": (POSITIONS["B"][0] + FACE - 160, POSITIONS["B"][1] + 128)}
    raw = tmp / "audio.wav"
    with wave.open(str(raw), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(sr)
        fh.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    args = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
            "-r", str(FPS), "-i", "pipe:0", "-i", str(raw)]
    if extra_audio:
        args += ["-f", "lavfi", "-t", str(duration), "-i", extra_audio,
                 "-filter_complex", "[1:a][2:a]amix=inputs=2:normalize=0[a]", "-map", "0:v", "-map", "[a]"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)]
    proc = subprocess.Popen(args, stdin=subprocess.PIPE)
    window = sr // FPS
    for k in range(int(duration * FPS)):
        frame = np.full((H, W, 3), (64, 48, 40), np.uint8)
        for name in visible:
            x, y = POSITIONS[name]
            frame[y:y + FACE, x:x + FACE] = face if name == "A" else mirrored
        a = k * window
        chunk = audio[a:a + window]
        loud = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
        speaker = owner[a + len(chunk) // 2] if len(chunk) else ""
        for name in visible:
            cx, cy = mouth[name]
            opening = int(min(22, loud * 160)) if speaker == name else 0
            cv2.ellipse(frame, (cx, cy), (26, 2 + opening), 0, 0, 360, (20, 10, 30), -1)
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    assert proc.wait() == 0
    return path

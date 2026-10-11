"""Speech activity from the audio track (offline, CPU).

Voice activity detection uses the Silero VAD model that ships inside faster-whisper (already
installed for captions; runs with onnxruntime on the CPU; no download). It gives the
probability that someone is speaking for every 32 ms of audio. A loudness envelope (RMS,
mapped to 0..1) is computed alongside: its rhythm is what mouth movement is compared with.

Voice activity says *that* speech is present. It does not say who speaks, and Silero can
also react to singing or speech-like sounds in music. Nothing here identifies a speaker.

The audio is decoded by FFmpeg to 16 kHz mono and aligned to the video's time zero
(``aresample=first_pts=0`` pads a late-starting audio stream with silence), then processed
in 30-second chunks so memory stays small for long videos.
"""

from __future__ import annotations

import logging
import math
import threading
import wave
from pathlib import Path
from typing import Callable

from ai_video_studio.core.errors import DependencyMissingError, JobCancelled, MediaError
from ai_video_studio.core.process import run_process
from ai_video_studio.reframe.model import SpeechActivity

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BLOCK = 512                    # samples per VAD decision (32 ms)
RATE = SAMPLE_RATE / BLOCK     # 31.25 values per second
CHUNK_BLOCKS = 937             # ~30 s per chunk
SILENCE_DB, LOUD_DB = -55.0, -12.0

Vad = Callable[[object], object]  # float32 samples (multiple of BLOCK) -> one probability per BLOCK


def speech_audio_args(ffmpeg: Path | str, source: str, output_wav: str) -> list[str]:
    return [
        str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-v", "error",
        "-i", str(source),
        "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-af", "aresample=async=1:first_pts=0",
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
        str(output_wav),
    ]


def extract_speech_audio(ffmpeg: Path | str, source: str, output_wav: str,
                         cancel_event: threading.Event | None = None) -> None:
    result = run_process(speech_audio_args(ffmpeg, source, output_wav), cancel_event=cancel_event)
    if result.returncode != 0:
        detail = result.stderr.lower()
        if "matches no streams" in detail or "does not contain any stream" in detail:
            raise MediaError("This video has no audio track, so speech cannot be analysed. "
                             "Use a face-only tracking mode.")
        last = next((ln.strip() for ln in reversed(result.stderr.splitlines()) if ln.strip()), "unknown error")
        raise MediaError(f"Audio extraction failed: {last}")


def silero_vad() -> tuple[Vad, str]:
    """The Silero VAD bundled with faster-whisper."""
    try:
        from faster_whisper.vad import get_vad_model
    except ImportError as exc:
        raise DependencyMissingError("faster-whisper is not installed, so speech cannot be analysed. "
                                     "Run: pip install -r requirements.txt") from exc
    model = get_vad_model()

    def run(samples):
        return model(samples).reshape(-1)

    return run, "Silero VAD (faster-whisper, CPU)"


def _energy(block_rms: float) -> float:
    db = 20 * math.log10(max(block_rms, 1e-9))
    return min(1.0, max(0.0, (db - SILENCE_DB) / (LOUD_DB - SILENCE_DB)))


def analyse_speech(wav_path: str, vad: Vad | None = None, detector: str = "",
                   progress: Callable[[float], None] | None = None,
                   check_cancelled: Callable[[], None] | None = None) -> SpeechActivity:
    import numpy as np

    if vad is None:
        vad, detector = silero_vad()
    probs: list[float] = []
    energy: list[float] = []
    try:
        with wave.open(wav_path, "rb") as wav:
            if wav.getframerate() != SAMPLE_RATE or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise MediaError("Unexpected audio format for speech analysis.")
            total = wav.getnframes()
            done = 0
            while True:
                if check_cancelled:
                    check_cancelled()
                raw = wav.readframes(CHUNK_BLOCKS * BLOCK)
                if not raw:
                    break
                samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
                pad = (-len(samples)) % BLOCK
                if pad:
                    samples = np.concatenate([samples, np.zeros(pad, np.float32)])
                probs += [float(p) for p in np.asarray(vad(samples)).reshape(-1)[: len(samples) // BLOCK]]
                rms = np.sqrt(np.mean(samples.reshape(-1, BLOCK) ** 2, axis=1))
                energy += [_energy(float(v)) for v in rms]
                done += len(raw) // 2
                if progress and total:
                    progress(min(1.0, done / total))
    except (wave.Error, EOFError) as exc:
        raise MediaError(f"The extracted audio could not be read: {exc}") from exc
    except JobCancelled:
        raise
    log.info("Speech analysed", extra={"seconds": round(len(probs) / RATE, 1),
                                       "speech_share": round(sum(p >= 0.5 for p in probs) / max(1, len(probs)), 3)})
    return SpeechActivity(rate=RATE, prob=probs, energy=energy, detector=detector)

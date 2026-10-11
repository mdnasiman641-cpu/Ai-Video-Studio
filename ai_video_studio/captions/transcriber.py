"""Speech-to-text with faster-whisper (CTranslate2).

GPU use: CTranslate2 needs CUDA 12 cuBLAS and cuDNN 9 libraries on Windows. Having an
NVIDIA GPU is not enough, so CUDA is tried first only when a CUDA device is visible,
and any CUDA failure (missing DLLs, out of memory, unsupported compute type) falls back
to the CPU with an int8 model. The fallback is reported in the result warnings.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ai_video_studio.captions.model import CaptionSegment, CaptionTrack, Word, clean_text, segments_from_words
from ai_video_studio.core.errors import DependencyMissingError, JobCancelled, StudioError

log = logging.getLogger(__name__)

# Whisper language codes. Whisper supports ~100 languages; these are the ones offered in the UI.
LANGUAGES: list[tuple[str, str]] = [
    ("auto", "Auto-detect"),
    ("en", "English"),
    ("bn", "Bengali (বাংলা)"),
    ("hi", "Hindi"),
    ("ur", "Urdu"),
    ("ar", "Arabic"),
    ("ne", "Nepali"),
    ("ta", "Tamil"),
    ("te", "Telugu"),
    ("mr", "Marathi"),
    ("es", "Spanish"),
    ("pt", "Portuguese"),
    ("fr", "French"),
    ("de", "German"),
    ("it", "Italian"),
    ("ru", "Russian"),
    ("tr", "Turkish"),
    ("id", "Indonesian"),
    ("ms", "Malay"),
    ("zh", "Chinese"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
]
LANGUAGE_CODES = {code for code, _ in LANGUAGES}

# faster-whisper model names. Larger = slower. For Bengali only large-v3 / large-v3-turbo produced
# Bengali text on real speech in testing (see documentation/ROADMAP.md).
MODELS = ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]
DEVICES = ("auto", "cuda", "cpu")


class TranscriptionError(StudioError):
    pass


@dataclass
class TranscriptionOptions:
    model: str = "small"
    language: str = "auto"           # "auto" or a Whisper language code
    device: str = "auto"             # auto | cuda | cpu
    models_dir: str | None = None    # where downloaded models are cached
    model_path: str | None = None    # optional local CTranslate2 model folder (offline use)
    max_chars: int = 42
    max_duration: float = 6.0
    beam_size: int = 5
    vad_filter: bool = True

    def validate(self) -> None:
        if self.language not in LANGUAGE_CODES:
            raise TranscriptionError(f"Unsupported language '{self.language}'")
        if self.device not in DEVICES:
            raise TranscriptionError(f"Unsupported device '{self.device}'")
        if not self.model_path and self.model not in MODELS:
            raise TranscriptionError(f"Unknown model '{self.model}'")
        if self.model_path:
            try:
                exists = Path(self.model_path).is_dir()
            except OSError as exc:  # e.g. WinError 448 (untrusted link) or access denied
                raise TranscriptionError(f"The model folder {self.model_path} cannot be read: "
                                         f"{exc.strerror or exc}. Choose another folder in Settings → Captions.") from exc
            if not exists:
                raise TranscriptionError(f"Model folder not found: {self.model_path}")


@dataclass
class TranscriptionResult:
    track: CaptionTrack
    language: str
    language_probability: float
    device: str
    compute_type: str
    model: str
    duration: float
    warnings: list[str] = field(default_factory=list)


ModelFactory = Callable[[str, str, str, str | None], object]


def register_nvidia_dll_dirs() -> list[str]:
    """Windows: make CUDA DLLs from the optional ``nvidia-cublas-cu12``/``nvidia-cudnn-cu12``
    pip packages (see requirements-gpu.txt) visible to CTranslate2. No-op elsewhere."""
    import os
    import sys

    if sys.platform != "win32":
        return []
    added = []
    try:
        import nvidia  # namespace package installed by the nvidia-*-cu12 wheels
    except ImportError:
        return []
    for base in getattr(nvidia, "__path__", []):
        for sub in ("cublas", "cudnn", "cuda_runtime"):
            folder = Path(base) / sub / "bin"
            if folder.is_dir():
                os.add_dll_directory(str(folder))
                os.environ["PATH"] = str(folder) + os.pathsep + os.environ.get("PATH", "")
                added.append(str(folder))
    _registered_dirs.extend(added)
    return added


_registered_dirs: list[str] = []


def register_nvidia_dll_dirs_list() -> list[str]:
    return list(_registered_dirs)


_dll_dirs_registered = False


def _ensure_dll_dirs() -> None:
    global _dll_dirs_registered
    if not _dll_dirs_registered:
        _dll_dirs_registered = True
        dirs = register_nvidia_dll_dirs()
        if dirs:
            log.info("Registered NVIDIA DLL folders", extra={"folders": dirs})


def _default_model_factory(model: str, device: str, compute_type: str, download_root: str | None):
    _ensure_dll_dirs()
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise DependencyMissingError(
            "faster-whisper is not installed. Run: pip install -r requirements.txt"
        ) from exc
    return WhisperModel(model, device=device, compute_type=compute_type, download_root=download_root)


# CTranslate2 4.x on Windows needs these from CUDA 12 / cuDNN 9. A GPU and driver alone are not
# enough, and a missing cuDNN can end the process inside the native library instead of raising an
# error, so "auto" only tries CUDA when the files are present.
CUDA_DLLS = ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll")


def _cuda_search_dirs() -> list[Path]:
    import os
    import sys

    dirs = [Path(d) for d in register_nvidia_dll_dirs_list()]
    try:
        import ctranslate2

        dirs.append(Path(ctranslate2.__file__).parent)
    except Exception:  # noqa: BLE001
        pass
    if getattr(sys, "_MEIPASS", None):
        dirs.append(Path(sys._MEIPASS))
    if os.environ.get("CUDA_PATH"):
        dirs.append(Path(os.environ["CUDA_PATH"]) / "bin")
    dirs += [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p.strip()]
    return dirs


def cuda_libraries() -> dict[str, str | None]:
    """Where each required CUDA DLL was found (None = missing). Only files are checked; nothing
    is loaded. Empty dict on platforms other than Windows (not checked there)."""
    import sys

    if sys.platform != "win32":
        return {}
    _ensure_dll_dirs()
    found: dict[str, str | None] = {name: None for name in CUDA_DLLS}
    for folder in _cuda_search_dirs():
        for name in CUDA_DLLS:
            if found[name] is None:
                try:
                    candidate = folder / name
                    if candidate.is_file():
                        found[name] = str(candidate)
                except OSError:
                    continue
    return found


def cuda_libraries_ok() -> bool:
    libs = cuda_libraries()
    return all(libs.values()) if libs else True


def cuda_device_count() -> int:
    _ensure_dll_dirs()
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count()
    except Exception:  # noqa: BLE001 - missing/broken CUDA runtime means "no GPU"
        return 0


def device_attempts(preference: str, cuda_devices: int, libraries_ok: bool = True) -> list[tuple[str, str]]:
    """Ordered (device, compute_type) attempts for a device preference.

    "auto" uses CUDA only when a device is visible **and** the CUDA libraries are present;
    "cuda" always tries CUDA first (the user asked for it). The CPU is always the last attempt.
    """
    cpu = ("cpu", "int8")
    if preference == "cpu":
        return [cpu]
    if preference == "cuda" or (cuda_devices > 0 and libraries_ok):
        return [("cuda", "float16"), cpu]
    return [cpu]


# One loaded model is kept for reuse across jobs; transcriptions run one at a time to
# avoid loading several large models into RAM/VRAM at once.
_engine_lock = threading.Lock()
_cached: dict = {}


def _get_model(factory: ModelFactory, name: str, device: str, compute_type: str, download_root: str | None):
    key = (factory, name, device, compute_type, download_root)
    if _cached.get("key") != key:
        _cached.clear()
        _cached["model"] = factory(name, device, compute_type, download_root)
        _cached["key"] = key
    return _cached["model"]


def clear_model_cache() -> None:
    with _engine_lock:
        _cached.clear()


def _friendly_load_error(exc: Exception, model: str) -> str:
    text = str(exc)
    lowered = text.lower()
    if any(s in lowered for s in ("connection", "proxy", "resolve", "timed out", "403", "offline", "localentrynotfound")):
        return (f"Could not download the Whisper model '{model}'. Check the internet connection, or download "
                f"a faster-whisper (CTranslate2) model manually and choose its folder in Settings. ({text})")
    return f"Could not load the Whisper model '{model}': {text}"


def transcribe_file(
    audio_path: str,
    options: TranscriptionOptions,
    progress: Callable[[float], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
    model_factory: ModelFactory | None = None,
    cuda_devices: int | None = None,
) -> TranscriptionResult:
    options.validate()
    factory = model_factory or _default_model_factory
    devices = cuda_device_count() if cuda_devices is None else cuda_devices
    libraries_ok = cuda_libraries_ok() if devices > 0 and options.device == "auto" else True
    attempts = device_attempts(options.device, devices, libraries_ok)
    model_name = options.model_path or options.model
    warnings: list[str] = []
    if not libraries_ok:
        missing = ", ".join(name for name, path in cuda_libraries().items() if not path)
        warnings.append(f"An NVIDIA GPU was found, but the CUDA libraries are missing ({missing}); "
                        "transcribing on the CPU. The standard build is CPU-only; see README → GPU and CPU.")
        log.info("CUDA libraries missing; using the CPU", extra={"missing": missing})
    if options.device == "cuda" and devices == 0:
        warnings.append("No CUDA device was detected; trying CUDA anyway as requested.")

    last_error: Exception | None = None
    with _engine_lock:
        for device, compute_type in attempts:
            if check_cancelled:
                check_cancelled()
            try:
                model = _get_model(factory, model_name, device, compute_type, options.models_dir)
            except (JobCancelled, StudioError):
                raise
            except Exception as exc:  # noqa: BLE001 - CUDA/DLL/download errors vary by platform
                last_error = exc
                if device == "cuda":
                    warnings.append(f"GPU (CUDA) could not be used, falling back to CPU: {exc}")
                    log.warning("CUDA model load failed; falling back to CPU", extra={"error": str(exc)})
                    continue
                raise TranscriptionError(_friendly_load_error(exc, model_name)) from exc
            try:
                result = _run(model, audio_path, options, progress, check_cancelled)
            except (JobCancelled, StudioError):
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                _cached.clear()
                if device == "cuda":
                    warnings.append(f"GPU (CUDA) transcription failed, retried on CPU: {exc}")
                    log.warning("CUDA transcription failed; retrying on CPU", extra={"error": str(exc)})
                    continue
                raise TranscriptionError(f"Transcription failed: {exc}") from exc
            result.device, result.compute_type, result.model = device, compute_type, model_name
            result.warnings = warnings + result.warnings
            log.info("Transcription finished", extra={"device": device, "model": model_name,
                                                      "segments": len(result.track.segments),
                                                      "language": result.language})
            return result
    raise TranscriptionError(f"Transcription failed: {last_error}")


def load_pcm16_wav(path: str):
    """Read a 16 kHz mono 16-bit WAV (what the app's FFmpeg extraction produces) as float32 samples.

    Passing samples instead of a file path means faster-whisper does not decode audio with
    PyAV. faster-whisper 1.2.1 calls ``av.open(..., metadata_errors=...)``, which PyAV 19
    removed, so path-based decoding fails with that combination (found by the real-Whisper
    CI tests). Returns ``None`` for any other format so the caller can fall back to the path.
    """
    import wave

    import numpy as np

    try:
        with wave.open(path, "rb") as wav:
            if wav.getframerate() != 16000 or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                return None
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError, OSError):
        return None
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


def _run(model, audio_path: str, options: TranscriptionOptions, progress, check_cancelled) -> TranscriptionResult:
    language = None if options.language == "auto" else options.language
    samples = load_pcm16_wav(audio_path)
    segments_iter, info = model.transcribe(
        samples if samples is not None else audio_path,
        language=language,
        beam_size=options.beam_size,
        word_timestamps=True,
        vad_filter=options.vad_filter,
    )
    duration = float(getattr(info, "duration", 0) or 0)
    words: list[Word] = []
    raw_segments: list[CaptionSegment] = []
    for seg in segments_iter:  # decoding happens lazily while iterating
        if check_cancelled:
            check_cancelled()
        seg_words = [Word(round(w.start, 3), round(w.end, 3), w.word) for w in (getattr(seg, "words", None) or [])]
        words.extend(seg_words)
        text = clean_text(seg.text)
        if text and seg.end - seg.start > 0:
            raw_segments.append(CaptionSegment(start=round(seg.start, 3), end=round(seg.end, 3),
                                               text=text, words=seg_words))
        if progress and duration > 0:
            progress(min(1.0, seg.end / duration))
    if words:
        segments = segments_from_words(words, options.max_chars, options.max_duration)
    else:
        segments = raw_segments
    detected = getattr(info, "language", None) or (language or "unknown")
    warnings = [] if segments else ["No speech was detected in the audio."]
    track = CaptionTrack(segments=segments, language=detected, source=f"whisper:{options.model_path or options.model}")
    return TranscriptionResult(
        track=track,
        language=detected,
        language_probability=float(getattr(info, "language_probability", 0) or 0),
        device="", compute_type="", model="",
        duration=duration,
        warnings=warnings,
    )

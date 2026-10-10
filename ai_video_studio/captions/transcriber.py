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

# faster-whisper model names. Larger = more accurate and slower. For Bengali, "medium" or
# larger is recommended; "tiny"/"base" are mainly useful for quick tests.
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
        if self.model_path and not Path(self.model_path).is_dir():
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
    return added


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


def cuda_device_count() -> int:
    _ensure_dll_dirs()
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count()
    except Exception:  # noqa: BLE001 - missing/broken CUDA runtime means "no GPU"
        return 0


def device_attempts(preference: str, cuda_devices: int) -> list[tuple[str, str]]:
    """Ordered (device, compute_type) attempts for a device preference."""
    cpu = ("cpu", "int8")
    if preference == "cpu":
        return [cpu]
    if preference == "cuda" or cuda_devices > 0:
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
    attempts = device_attempts(options.device, devices)
    model_name = options.model_path or options.model
    warnings: list[str] = []
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


def _run(model, audio_path: str, options: TranscriptionOptions, progress, check_cancelled) -> TranscriptionResult:
    language = None if options.language == "auto" else options.language
    segments_iter, info = model.transcribe(
        audio_path,
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

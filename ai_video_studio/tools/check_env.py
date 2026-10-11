"""Verify the runtime environment: ``python -m ai_video_studio.tools.check_env``.

Exit code 0 when everything required is present, 1 otherwise.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from ai_video_studio.core.ffmpeg_tools import find_tool, tool_version

MIN_PYTHON = (3, 10)


def _line(ok: bool | None, label: str, detail: str = "") -> None:
    mark = {True: "[ OK ]", False: "[FAIL]", None: "[INFO]"}[ok]
    print(f"{mark} {label}" + (f": {detail}" if detail else ""))


def main() -> int:
    from ai_video_studio.app import ensure_std_streams

    ensure_std_streams()
    failures = 0

    py_ok = sys.version_info[:2] >= MIN_PYTHON
    _line(py_ok, "Python", sys.version.split()[0] + ("" if py_ok else f" (need {'.'.join(map(str, MIN_PYTHON))}+)"))
    failures += not py_ok
    _line(None, "Platform", sys.platform + (" (64-bit)" if sys.maxsize > 2**32 else " (32-bit)"))

    for module in ("PySide6", "PySide6.QtWidgets", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets"):
        try:
            mod = importlib.import_module(module)
            _line(True, module, getattr(mod, "__version__", ""))
        except Exception as exc:  # noqa: BLE001
            _line(False, module, str(exc))
            failures += 1

    try:
        import faster_whisper

        _line(True, "faster-whisper", faster_whisper.__version__)
        from ai_video_studio.captions.transcriber import cuda_device_count

        from ai_video_studio.captions.transcriber import cuda_libraries, device_attempts

        gpus = cuda_device_count()
        _line(None, "CUDA devices visible to CTranslate2",
              f"{gpus}" + ("" if gpus else " (speech recognition will use the CPU)"))
        libs = cuda_libraries()
        if libs:
            missing = [name for name, path in libs.items() if not path]
            _line(None, "CUDA 12 cuBLAS / cuDNN 9 libraries",
                  "all found" if not missing else f"missing: {', '.join(missing)} (standard CPU build: expected)")
        else:
            missing = []
        first = device_attempts("auto", gpus, not missing)[0][0]
        _line(None, "Transcription device with 'Automatic'",
              "CUDA (falls back to the CPU if CUDA fails)" if first == "cuda" else "CPU")
    except Exception as exc:  # noqa: BLE001
        _line(False, "faster-whisper", str(exc))
        failures += 1

    from ai_video_studio.reframe.detection import YUNET_FILE, opencv_cuda_devices, opencv_version

    cv_version = opencv_version()
    if cv_version is None:
        _line(False, "OpenCV (face/person detection)", "not installed; run: pip install -r requirements.txt")
        failures += 1
    else:
        _line(True, "OpenCV (face/person detection)", cv_version)
        cuda = opencv_cuda_devices()
        _line(None, "Face detection device",
              f"CUDA ({cuda} device(s))" if cuda else "CPU (pip OpenCV builds have no CUDA; this is expected)")
        from ai_video_studio.core import paths

        bundled = paths.app_root() / "models" / YUNET_FILE
        model = paths.data_dir() / "models" / YUNET_FILE
        _line(None, "YuNet face model", f"{bundled} (bundled)" if bundled.is_file() else str(model)
              if model.is_file() else "not downloaded yet (downloaded automatically on the first analysis)")

    try:
        from ai_video_studio.reframe.speech import silero_vad

        _vad, name = silero_vad()
        _line(True, "Speech activity (Active Speaker / Speech-Audio modes)", name)
    except Exception as exc:  # noqa: BLE001
        _line(False, "Speech activity (Active Speaker / Speech-Audio modes)", str(exc))
        failures += 1

    from ai_video_studio.core import paths
    from ai_video_studio.storage import secrets

    _line(None, "Packaged program" if paths.is_frozen() else "Running from source",
          str(paths.exe_dir()) + (" (portable mode: portable.txt)" if paths.portable_data_dir() else ""))
    for label, folder in (("Data folder", paths.data_dir()), ("Logs", paths.logs_dir()),
                          ("Models", paths.models_dir()), ("Temporary files", paths.temp_dir())):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            _line(True, label, f"{folder} (writable)")
        except OSError as exc:
            _line(False, label, f"{folder} is not writable: {exc}")
            failures += 1

    _line(None, "Secure API-key storage", "available (Windows Credential Manager)" if secrets.secure_store_available()
          else "not available (use environment variables; see the AI Models page)")

    configured = None
    try:
        from ai_video_studio.services import Services

        services = Services()
        configured = services.settings.get("ffmpeg_dir") or None
        services.close()
    except Exception as exc:  # noqa: BLE001
        _line(False, "Application database", str(exc))
        failures += 1

    for name in ("ffmpeg", "ffprobe"):
        path = find_tool(name, configured)
        if path is None:
            _line(False, name, "not found (install with 'winget install Gyan.FFmpeg' or set the folder in Settings)")
            failures += 1
        else:
            _line(True, name, f"{path} — {tool_version(path) or 'version unknown'}")

    ffmpeg = find_tool("ffmpeg", configured)
    if ffmpeg:
        from ai_video_studio.core.ffmpeg_commands import available_encoders, nvenc_usable

        bundled = any(Path(ffmpeg).parent == root / "bin" for root in (paths.exe_dir(), paths.app_root()))
        _line(None, "FFmpeg source", "folder chosen in Settings" if configured and Path(ffmpeg).parent == Path(
            configured) else "bundled with the program" if bundled else "PATH")
        encoders = available_encoders(ffmpeg)
        _line(None, "libx264 / libx265 (CPU encoders)",
              f"{'yes' if 'libx264' in encoders else 'NO'} / {'yes' if 'libx265' in encoders else 'no'}")
        # Listing an encoder only means FFmpeg was built with it; a 1-frame test encode proves that
        # this GPU and driver can use it. Export uses NVENC only when that test succeeds.
        for name in ("h264_nvenc", "hevc_nvenc"):
            if name not in encoders:
                _line(None, f"{name}", "not in this FFmpeg build")
            else:
                usable = nvenc_usable(ffmpeg, name)
                _line(None, f"{name}", "listed; test encode OK (exports can use it)" if usable
                      else "listed, but the test encode failed (no usable NVIDIA GPU/driver): CPU is used")

    print()
    print("Environment OK" if failures == 0 else f"{failures} problem(s) found")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

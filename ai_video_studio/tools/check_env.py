"""Verify the runtime environment: ``python -m ai_video_studio.tools.check_env``.

Exit code 0 when everything required is present, 1 otherwise.
"""

from __future__ import annotations

import importlib
import sys

from ai_video_studio.core.ffmpeg_tools import find_tool, tool_version
from ai_video_studio.core.process import run_process

MIN_PYTHON = (3, 10)


def _line(ok: bool | None, label: str, detail: str = "") -> None:
    mark = {True: "[ OK ]", False: "[FAIL]", None: "[INFO]"}[ok]
    print(f"{mark} {label}" + (f": {detail}" if detail else ""))


def main() -> int:
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

        gpus = cuda_device_count()
        _line(None, "CUDA devices visible to CTranslate2",
              f"{gpus}" + ("" if gpus else " (speech recognition will use the CPU)"))
    except Exception as exc:  # noqa: BLE001
        _line(False, "faster-whisper", str(exc))
        failures += 1

    from ai_video_studio.storage import secrets

    _line(None, "Secure API-key storage", "available" if secrets.secure_store_available()
          else "not available (use GEMINI_API_KEY / OPENAI_API_KEY environment variables)")

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
        # Informational only: listing an encoder does not prove the GPU/driver can use it.
        result = run_process([str(ffmpeg), "-hide_banner", "-encoders"], timeout=20)
        nvenc = [e for e in ("h264_nvenc", "hevc_nvenc", "av1_nvenc") if e in result.stdout]
        _line(None, "NVENC encoders in this FFmpeg build", ", ".join(nvenc) or "none")

    print()
    print("Environment OK" if failures == 0 else f"{failures} problem(s) found")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

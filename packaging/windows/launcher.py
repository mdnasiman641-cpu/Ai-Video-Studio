"""Entry point of the packaged executables (AI-Video-Studio.exe and AI-Video-Studio-Console.exe).

The windowed EXE has no console; start-up failures are written to
%LOCALAPPDATA%\\AIVideoStudio\\startup_error.log (see ai_video_studio.__main__).
"""

from ai_video_studio.__main__ import _run

if __name__ == "__main__":
    raise SystemExit(_run())

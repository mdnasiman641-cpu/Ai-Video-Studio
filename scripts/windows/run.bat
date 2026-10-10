@echo off
REM Start AI Video Studio. Pass --console to keep a console window with live logs.
setlocal
cd /d "%~dp0\..\.."
if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found. Run scripts\windows\setup.bat first.
    pause
    exit /b 1
)
if /i "%1"=="--console" (
    ".venv\Scripts\python.exe" -m ai_video_studio %2 %3
) else (
    start "" ".venv\Scripts\pythonw.exe" -m ai_video_studio %*
)

@echo off
setlocal
cd /d "%~dp0\..\.."
".venv\Scripts\python.exe" -m ai_video_studio.tools.check_env
pause

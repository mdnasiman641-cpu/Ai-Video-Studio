@echo off
REM Run the automated test suite (installs dev dependencies on first use).
setlocal
cd /d "%~dp0\..\.."
if not exist ".venv\Scripts\python.exe" call scripts\windows\setup.bat --dev
".venv\Scripts\python.exe" -m pip install -q -r requirements-dev.txt || exit /b 1
".venv\Scripts\python.exe" -m pytest %*

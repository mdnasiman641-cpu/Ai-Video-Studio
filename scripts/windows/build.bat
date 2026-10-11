@echo off
REM Build the Windows application: dist\AI-Video-Studio\AI-Video-Studio.exe, the portable ZIP and,
REM if Inno Setup 6 is installed (winget install JRSoftware.InnoSetup), the installer.
REM Uses a separate virtual environment (.venv-build) with the locked dependency versions.
REM Options are passed to packaging\build.py, for example:  build.bat --ffmpeg none
setlocal
cd /d "%~dp0\..\.."

where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py -3.12"
) else (
    set "PY=python"
)
if not exist ".venv-build\Scripts\python.exe" (
    echo Creating build environment in .venv-build ...
    %PY% -m venv .venv-build || goto :error
)
".venv-build\Scripts\python.exe" -m pip install --upgrade pip || goto :error
".venv-build\Scripts\python.exe" -m pip install -r requirements-build.txt -c packaging\windows\constraints.txt || goto :error
".venv-build\Scripts\python.exe" packaging\build.py %* || goto :error
echo.
echo Done. Start dist\AI-Video-Studio\AI-Video-Studio.exe
echo Test the packaged program:  powershell -ExecutionPolicy Bypass -File packaging\windows\test_packaged.ps1 -AppDir dist\AI-Video-Studio
exit /b 0

:error
echo.
echo Build failed. See the messages above.
exit /b 1

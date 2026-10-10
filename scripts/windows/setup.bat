@echo off
REM One-time setup: creates .venv, installs dependencies and checks the environment.
REM Options: --dev (test tools)  --gpu (CUDA libraries for GPU speech recognition)
setlocal
cd /d "%~dp0\..\.."

where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py -3"
) else (
    set "PY=python"
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment in .venv ...
    %PY% -m venv .venv || goto :error
)

echo Installing dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade pip || goto :error
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
for %%A in (%*) do (
    if /i "%%A"=="--dev" ".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt || goto :error
    if /i "%%A"=="--gpu" ".venv\Scripts\python.exe" -m pip install -r requirements-gpu.txt || goto :error
)

echo.
echo Checking environment ...
".venv\Scripts\python.exe" -m ai_video_studio.tools.check_env
if errorlevel 1 (
    echo.
    echo Setup finished, but some requirements are missing. See the messages above.
    echo FFmpeg can be installed with:  winget install Gyan.FFmpeg
    exit /b 1
)
echo.
echo Setup complete. Start the app with scripts\windows\run.bat
exit /b 0

:error
echo.
echo Setup failed. See the messages above.
exit /b 1

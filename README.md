# AI Video Studio

A Windows desktop video editor (Python + PySide6 + FFmpeg) built for 1080p
short-form work: captions, face-tracked reframing, silence removal, resizing and
batch export. It is being built in phases. **Phase 1 (application foundation) is
done.** Later features are listed in the app as "planned" and are not presented
as working.

## Status

| Phase | Scope | Status |
|------|-------|--------|
| 0 | Repository audit and plan | Done ([documentation/ROADMAP.md](documentation/ROADMAP.md)) |
| 1 | Foundation: UI shell, import, metadata, preview, projects, SQLite, jobs, settings, logging | **Done** |
| 2 | Caption generation and editing | Planned |
| 3 | Face tracking and auto-reframe | Planned |
| 4 | Silence removal and manual cutting | Planned |
| 5 | Resize and visual editing | Planned |
| 6 | Bulk editing and batch export | Planned |
| 7 | Gemini / OpenAI provider manager | Planned |
| 8 | Hardware optimisation (NVENC, monitoring) | Planned |
| 9 | Windows build and release (PyInstaller) | Planned |

### What works now (Phase 1)

- Dark-themed main window with the 11-section sidebar (Home, Media, Edit, Captions, Face Tracking,
  Smart Cut, Resize & Crop, Bulk Processing, Export Queue, AI Models, Settings).
- Import videos with the file dialog, a whole folder (recursive), or drag-and-drop onto the media
  list or anywhere on the window. Duplicates and unsupported or empty files are rejected with a reason.
- Metadata read by FFprobe in background jobs: duration, resolution (taking rotation into account),
  fps, codecs, pixel format, bit rate, audio format and size.
- Video preview (Qt Multimedia) with play/pause, stop, seeking, frame stepping (←/→), volume and mute.
  A clip only loads while the Edit page is visible, and closing the project releases the file.
- Projects saved as portable `.avsproj` JSON. Saves are atomic (a crash can't leave a half-written
  file). If the project folder moves together with its media, relative paths relink the media.
  Missing files are flagged. The app keeps a recent-projects list, asks about unsaved changes,
  and can reopen the last project on startup.
- SQLite application database for settings, recent projects and job history, with versioned
  migrations.
- Persistent job queue with the states queued, running, completed, failed and cancelled. It enforces
  which state changes are allowed, has a configurable concurrency limit, pause/resume, and
  cancel/retry/clear. After a crash, jobs that were running are marked failed and can be retried.
  Queued jobs survive a restart.
- Settings: FFmpeg folder, projects folder, max concurrent jobs, preview volume, log level and
  reopen-last-project. All are validated before saving.
- Structured JSON logs with rotation. API keys and tokens are redacted. Unhandled errors are logged
  and shown in a dialog.

## Requirements

- Windows 10/11 64-bit
- Python 3.10–3.13 (64-bit) from python.org
- FFmpeg + FFprobe, for example `winget install Gyan.FFmpeg`. You can also set the folder that
  contains `ffmpeg.exe` in Settings, or put the binaries in `bin\` next to the app.

## Setup and run (Windows)

```bat
git clone https://github.com/mdnasiman641-cpu/Ai-Video-Studio.git
cd Ai-Video-Studio
scripts\windows\setup.bat          :: creates .venv, installs deps, checks environment
scripts\windows\run.bat            :: start the app
scripts\windows\run.bat --console  :: start with a console showing live logs
scripts\windows\run_tests.bat      :: run the test suite
scripts\windows\check_env.bat      :: re-check Python, PySide6, FFmpeg, NVENC
```

Manual equivalent:

```bat
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m ai_video_studio.tools.check_env
.venv\Scripts\python -m ai_video_studio [optional\path\project.avsproj]
.venv\Scripts\python -m pytest
```

## Where data is stored

| What | Location |
|------|----------|
| Settings, recent projects, job history | `%LOCALAPPDATA%\AIVideoStudio\studio.db` |
| Logs (JSON lines, rotated 5 × 5 MB) | `%LOCALAPPDATA%\AIVideoStudio\logs\` |
| Projects | wherever you save them (default `Documents\AI Video Studio Projects`) |

Set `AVS_DATA_DIR` to use another data folder, for example for a portable install. Source videos
are never modified.

## Documentation

- [documentation/ARCHITECTURE.md](documentation/ARCHITECTURE.md): modules, threading and data flow
- [documentation/ROADMAP.md](documentation/ROADMAP.md): audit, phase plan, risks and dependency notes
- [documentation/TROUBLESHOOTING.md](documentation/TROUBLESHOOTING.md)
- [CHANGELOG.md](CHANGELOG.md)

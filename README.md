# AI Video Studio

A Windows desktop video editor (Python + PySide6 + FFmpeg) built for 1080p
short-form work: captions, face-tracked reframing, silence removal, resizing and
batch export. It is being built in phases. **Phases 1 (foundation) and 2
(captions) are done.** Later features are listed in the app as "planned" and are
not presented as working.

## Status

| Phase | Scope | Status |
|------|-------|--------|
| 0 | Repository audit and plan | Done ([documentation/ROADMAP.md](documentation/ROADMAP.md)) |
| 1 | Foundation: UI shell, import, metadata, preview, projects, SQLite, jobs, settings, logging | **Done** |
| 2 | Caption generation, editing, styling, SRT, burn-in, optional AI correction | **Done** (see limitations) |
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

### What works now (Phase 2: captions)

- **Generate captions** with faster-whisper (models tiny → large-v3 / large-v3-turbo) for one or
  many videos. You choose the language (auto-detect, English, Bengali, Hindi, Urdu and others) and
  the device (automatic, CUDA or CPU).
  - FFmpeg first extracts the audio as 16 kHz mono WAV in a background job.
  - Word timestamps are regrouped into readable captions, using the max characters and max seconds
    set in Settings.
  - On CUDA errors (missing cuBLAS/cuDNN, out of memory) the job retries on the CPU and the
    fallback is reported.
- **Caption editor**:
  - Edit text (multi-line) and start/end times (`mm:ss.mmm`) in place, with validation.
  - Add or split at the playhead, merge neighbouring captions, delete.
  - Undo/redo (Ctrl+Z / Ctrl+Y).
  - Overlapping captions are highlighted, and selecting a row seeks the preview.
- **SRT import/export.** Import copes with a BOM, CRLF line endings, `.` milliseconds, missing
  indexes, formatting tags and UTF-8/UTF-16/cp1252 files. Bad blocks are skipped and reported.
- **Styling**: font, size, bold/italic, text colour, outline colour and width, shadow colour,
  offset and opacity, alignment, position (top/middle/bottom), margins, background box, and
  characters per line / max lines. Sizes scale with the video height.
- **Live preview** of the styled captions over the playing video. **Exact Preview** renders the
  current frame through FFmpeg/libass, exactly as it will be exported.
- **Presets**: five built-ins plus your own saved presets (stored in SQLite). You can apply a
  preset, or copy the current style, to any selection of videos.
- **Burn-in export** to MP4 with FFmpeg/libass:
  - Uses NVENC when a real test encode succeeds, otherwise x264 (configurable). If NVENC fails
    during the export, it is retried with x264.
  - AAC audio is stream-copied; other audio codecs are re-encoded to AAC.
  - Output is written to `*.part.mp4` and renamed when finished. The source file is never
    overwritten.
- **Optional AI correction** with Gemini or OpenAI. It is off by default and sends caption text
  only. Suggestions are shown side by side with word diffs, unchecked by default. Only the ones
  you accept are applied (with undo), and captions you edited after the check are skipped.
- **Cut sync**: `captions.model.remap_to_cuts` maps captions onto an edited timeline. Captions in
  removed parts are dropped and partly cut ones are trimmed, along with the words inside the cut.
  Phase 4's cutting feature will use it. There is no cutting UI yet.
- Captions and styles are saved in the project file (format version 2; version 1 files still open).
  A transcription that finishes while its project is closed is applied the next time the project
  is opened, unless that video already has captions.

## Requirements

- Windows 10/11 64-bit
- Python 3.10–3.13 (64-bit) from python.org
- FFmpeg + FFprobe, for example `winget install Gyan.FFmpeg`. You can also set the folder that
  contains `ffmpeg.exe` in Settings, or put the binaries in `bin\` next to the app. Burn-in needs
  an FFmpeg build with libass (the Gyan and BtbN Windows builds include it).
- Internet access the first time you transcribe, to download the Whisper model from Hugging Face
  (about 75 MB for tiny, about 3 GB for large-v3). Alternatively, download a faster-whisper
  (CTranslate2) model yourself and choose its folder in Settings.
- Optional GPU transcription: `scripts\windows\setup.bat --gpu` installs the CUDA 12 cuBLAS and
  cuDNN 9 runtime wheels.
- Optional AI correction: a Gemini or OpenAI API key, set through the `GEMINI_API_KEY` /
  `OPENAI_API_KEY` environment variables or stored from Settings in the Windows Credential Manager.

## Setup and run (Windows)

```bat
git clone https://github.com/mdnasiman641-cpu/Ai-Video-Studio.git
cd Ai-Video-Studio
scripts\windows\setup.bat          :: creates .venv, installs deps, checks environment
scripts\windows\setup.bat --gpu    :: also install CUDA libraries for GPU speech recognition
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
| Whisper models | `%LOCALAPPDATA%\AIVideoStudio\models\` |
| Caption presets | in `studio.db` |
| API keys | environment variables or Windows Credential Manager (never in files or logs) |

Set `AVS_DATA_DIR` to use another data folder, for example for a portable install. Source videos
are never modified.

## Documentation

- [documentation/ARCHITECTURE.md](documentation/ARCHITECTURE.md): modules, threading and data flow
- [documentation/ROADMAP.md](documentation/ROADMAP.md): audit, phase plan, risks and dependency notes
- [documentation/TROUBLESHOOTING.md](documentation/TROUBLESHOOTING.md)
- [CHANGELOG.md](CHANGELOG.md)

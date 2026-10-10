# Changelog

All notable changes are listed here. Versions follow semantic versioning.

## [0.2.1] - Unreleased

### Added
- GitHub Actions CI on `windows-latest` and `ubuntu-latest`:
  - full test suite with real faster-whisper models (tiny and small)
  - app smoke test with the native Windows platform and under `pythonw.exe`
  - `setup.bat`, `run.bat` and `run_tests.bat` executed on Windows
- `--smoke-test` start-up mode.
- Real-Whisper integration tests (synthetic speech fixtures in `tests/fixtures`), hardware detection
  tests, and a real credential-store test. CI fails, rather than skips, when the model is missing.
- Bengali + tiny/base warning in the Generate Captions dialog.
- `startup_error.log` when start-up fails without a console.

### Fixed (found by real tests)
- **Real transcription always failed with PyAV 19** ("unexpected keyword argument 'metadata_errors'"
  inside faster-whisper 1.2.1). The transcriber now reads its own 16 kHz WAV and passes the samples
  directly, and `av<19` is pinned.
- **Caption generation could crash when started with `run.bat`**. `pythonw.exe` has no stdout/stderr,
  and the model download progress bar wrote to them. Missing streams are now replaced, and console
  output that cannot be encoded is escaped instead of raising.
- **Windows: cancelling an export left FFmpeg running and the `.part.mp4` behind**. The child of an
  FFmpeg launcher (e.g. a Chocolatey shim) survived the kill. The whole process tree is now killed
  (`taskkill /T`), and deletes retry while Windows releases file locks.

## [0.2.0] - Unreleased

### Added (Phase 2: captions)
- Speech-to-text with faster-whisper as a background job: FFmpeg audio extraction, language
  choice (including Bengali), model choice, automatic CUDA → CPU fallback, progress and
  cancellation, and word-timestamp regrouping into readable captions.
- Caption editor: in-place text and time editing with validation; add, split, merge and delete;
  undo/redo; overlap highlighting; playhead sync.
- SRT import (tolerant parser with warnings) and export.
- Caption styles (font, colours, outline, shadow, alignment, position, margins, background box,
  line wrapping), built-in and user presets, and applying a style to selected videos.
- Live caption overlay on the video preview (video is now painted from a QVideoSink).
- Exact single-frame preview rendered by FFmpeg/libass.
- Burn-in export job: ASS generation, NVENC with a verified test encode and x264 fallback,
  `.part` output file, and the source is never overwritten.
- Optional Gemini/OpenAI caption correction with review: request batching, error classification,
  retries only for rate-limit, server and network errors, word diffs, stale-suggestion detection.
- `remap_to_cuts` to keep captions in sync once cutting arrives (Phase 4).
- Settings for the Whisper model, local model folder, device, default language, caption length
  limits, video encoder and quality, AI provider, model names and timeout, and API keys (env vars
  or the OS credential store).
- Project format v2 (per-video `captions` and `caption_style`); v1 projects still load.
- Database migration 2 (`caption_presets`).
- `requirements-gpu.txt` and `setup.bat --gpu`. Windows DLL-folder registration for the
  pip-installed CUDA libraries.

### Changed
- The Captions sidebar entry is now a working page.
- The video preview draws frames itself so overlays can be painted on top.

## [0.1.0] - Unreleased

### Added (Phase 1: application foundation)
- PySide6 main window with dark theme and an 11-section sidebar. Sections for later phases say
  they are planned and have no controls.
- Video import via dialog, folder, and drag-and-drop, with validation and duplicate protection.
- FFprobe metadata extraction in background jobs, including rotation-aware display size.
- Video preview with transport controls and frame stepping.
- `.avsproj` project files: atomic save, open, save-as, recent projects, relinking via relative
  paths, missing-file detection, unsaved-changes prompts, reopen last project.
- SQLite database with migrations for settings, recent projects and jobs.
- Persistent job manager with validated state changes, concurrency limit, pause/resume,
  cancel/retry/clear, and recovery after a crash.
- Validated settings page and FFmpeg detection.
- Structured JSON logging with secret redaction, plus global exception reporting.
- Environment checker (`python -m ai_video_studio.tools.check_env`) and Windows setup/run/test scripts.
- Unit, integration (real FFprobe on generated clips) and offscreen GUI tests.

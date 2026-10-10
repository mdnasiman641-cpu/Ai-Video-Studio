# Changelog

All notable changes are listed here. Versions follow semantic versioning.

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

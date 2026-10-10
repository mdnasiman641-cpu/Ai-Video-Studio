# Changelog

All notable changes are listed here. Versions follow semantic versioning.

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

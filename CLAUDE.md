# CLAUDE.md: notes for AI assistants working on this repo

## Project
AI Video Studio is a Windows desktop video editor (Python, PySide6, FFmpeg). Development follows the
phases in `documentation/ROADMAP.md`. Implement one phase at a time and verify it before moving on.

## Rules
- Never add buttons that do nothing, fake progress, or simulated processing. A feature that is not
  implemented belongs on its `PlannedPage` with no controls.
- Never modify source videos. Write outputs and temp files elsewhere (`core/paths.py`).
- Never commit secrets, videos, model weights, venvs or build output (see `.gitignore`). Log through
  `logging`; `core/logging_setup.redact` masks keys and tokens.
- Long-running work runs as a job (`jobs/`), never on the GUI thread. Handlers are plain functions
  `(job, ctx) -> dict` registered in `services.py`. They must call `ctx.report_progress()` or
  `ctx.check_cancelled()` regularly and run subprocesses with `core.process.run_process(...,
  cancel_event=ctx.cancel_event)`.
- Keep `core/`, `storage/` and `jobs/` free of Qt imports so they stay unit-testable. Qt code lives
  in `ui/`.
- Raise `StudioError` subclasses for expected, user-facing failures. Their messages are shown
  to users.
- Schema changes: append a new entry to `storage/database.MIGRATIONS` and never edit old ones.
  Changing the project file format means bumping `PROJECT_VERSION` and keeping old files loadable.
- Do not assume CUDA/NVENC. Detect capabilities and keep a CPU fallback.
- Job payloads must snapshot everything a job needs (e.g. the generated ASS text), so queued jobs
  survive restarts and are not affected by later edits.
- AI suggestions are never applied without user review. Never send media to cloud providers.

## Commands
```bash
pip install -r requirements-dev.txt
python -m pytest                       # all tests (GUI tests run offscreen)
python -m ai_video_studio              # run the app
python -m ai_video_studio.tools.check_env
```
On headless Linux, set `QT_QPA_PLATFORM=offscreen`. Whisper and the AI providers are faked in tests
(`tests/fakes.py`, injected transports). The real-Whisper test only runs when
`AVS_TEST_WHISPER_MODEL_DIR` points to a local model. Tests generate tiny sample videos with FFmpeg
at runtime, so no media is committed. Tests that need FFmpeg are skipped when it is not installed.

## Layout
- `ai_video_studio/core/`: paths, logging, errors, subprocess (`run_process`, `run_streaming`),
  ffmpeg/ffprobe, FFmpeg command builders and encoder selection (`ffmpeg_commands.py`), media model
- `ai_video_studio/captions/`: caption model and edit operations, SRT, style, ASS generation,
  faster-whisper transcriber, AI correction (no Qt)
- `ai_video_studio/ai/`: provider profiles (`profiles.py`), HTTP client (`client.py`), capability-aware
  router with retries/fallback (`router.py`), usage log (`usage.py`); stdlib HTTP, injectable transport
- `ai_video_studio/smartcut/`: audio levels (FFmpeg astats) and silence detection / cut lists (no Qt)
- `ai_video_studio/render/`: export settings, speed and audio effects (`effects.py`), cut/stage/speed/subtitle
  filter graph (`pipeline.py`), resize layout used by preview and export (`layout.py`), batch presets and
  payloads, safe output names
- `ai_video_studio/storage/`: SQLite database, settings, project files and recent projects
- `ai_video_studio/jobs/`: job model, persistent store, manager (heavy-render limit, held state, stages),
  handlers (`caption_jobs.py`, `reframe_jobs.py`, `render_jobs.py` for Smart Cut analysis and the shared
  `render_video` export, `ai_jobs.py` for AI tests and title suggestions)
- `ai_video_studio/services.py`: wires storage, settings and jobs together
- `ai_video_studio/ui/`: main window, pages, widgets, theme, project controller
- `tests/unit`, `tests/integration` (real FFmpeg), `tests/ui` (pytest-qt, offscreen)

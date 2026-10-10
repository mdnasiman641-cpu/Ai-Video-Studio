# Architecture

## Layers

```
ui/            PySide6 widgets: main window, pages, preview, theme
  │  signals/slots on the GUI thread
  ▼
ui/project_controller.py   open project + JobBridge (worker thread → GUI thread)
  │
services.py    wires the pieces together (one instance per app run)
  ├── storage/database.py   SQLite (WAL), versioned migrations, thread-safe
  ├── storage/settings.py   validated key/value settings
  ├── storage/project.py    Project model, .avsproj JSON, recent projects
  └── jobs/                 persistent queue + worker threads
        ├── models.py       JobState + allowed transitions
        ├── store.py        SQLite persistence, atomic transitions, crash recovery
        ├── manager.py      dispatch, concurrency limit, pause/resume, cancel, retry
        └── handlers.py     job implementations (Phase 1: FFprobe metadata)
core/          no Qt: paths, logging, errors, process runner, ffmpeg/ffprobe, media model
```

`core/`, `storage/` and `jobs/` never import Qt, so they can be tested without a display and
reused by a future command-line or batch runner.

## Threading

- The GUI thread only does UI work.
- `JobManager` runs handlers on a `ThreadPoolExecutor` (up to 8 threads). `max_concurrent_jobs`
  limits how many jobs run at once. Rendering concurrency gets its own setting in Phase 6/8.
- Handlers start external tools with `core.process.run_process`. When the job's cancel event is
  set, it kills the child process.
- Listener callbacks fire on worker threads. `JobBridge` re-emits them as a Qt signal, and because
  the receivers live on the GUI thread, Qt delivers them as queued calls.

## Job lifecycle

```
QUEUED ──► RUNNING ──► COMPLETED
  │          ├──────► FAILED ────┐
  │          └──────► CANCELLED ─┤ (retry)
  └──► CANCELLED                 └──► QUEUED
```

- `JobStore.transition` checks the state change inside a SQLite transaction and rejects illegal
  ones with `InvalidStateTransition`.
- On startup, `JobManager.recover()` marks jobs left `RUNNING` (crash or forced exit) as `FAILED`
  with an "Interrupted" message. `QUEUED` jobs resume.
- A queued job whose type no longer has a handler fails instead of blocking the queue.

## Project files (`.avsproj`)

JSON with `format`, `version`, project id/name/timestamps, and a media list. Each media entry
holds the absolute path, the path relative to the project file, and cached metadata. Writes go to a
temp file in the same folder, are fsynced, then `os.replace`d. Newer, unknown or damaged files are
rejected with a clear message. Per-video edit settings (captions, crops, cuts) will be added to each
media entry in later phases, with a version bump.

## Error handling

- Expected failures raise `StudioError` subclasses and their message is shown to the user.
- Unexpected exceptions in job handlers are caught, logged with a traceback, and the job is marked
  failed. The queue keeps running.
- `sys.excepthook` and `threading.excepthook` log anything unhandled. On the GUI thread, the user
  also sees a dialog.

## Logging

`%LOCALAPPDATA%\AIVideoStudio\logs\ai_video_studio.log` holds one JSON object per line with
time, level, logger, message and context fields. Values whose keys look like secrets
(`api_key`, `token`, ...) and strings that look like OpenAI/Google keys are replaced with `***`.

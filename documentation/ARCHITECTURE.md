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
captions/      no Qt: caption model + edits, SRT, style, ASS, transcriber, AI correction
reframe/       no Qt: detections/tracks model, OpenCV detectors, tracker, analysis, crop path, sendcmd export
ai/            no Qt: Gemini/OpenAI JSON clients
```

## Caption pipeline (Phase 2)

```
video ──ffmpeg──► 16 kHz mono WAV ──faster-whisper──► words ──segments_from_words──► CaptionTrack
                      (temp, deleted)                                                     │
SRT file ─────────────────────────────parse_srt──────────────────────────────────────────┤
                                                                                          ▼
                                                    editor (copy → edit → undo snapshot) ─┤
                                                                                          ▼
         CaptionStyle ──► wrap_text (same line breaks everywhere)                        │
             │                     │                                                     │
             ▼                     ▼                                                     ▼
   QPainter overlay (live)    build_ass ──► captions.ass ──ffmpeg subtitles=(libass)──► MP4
                                            (frame preview job renders 1 PNG the same way)
```

- **Sizes** are in pixels for a 1080-pixel-tall frame and scaled to the video height. The ASS file
  uses `PlayResX/Y` = the video's display size, so libass and the overlay agree.
- **Line breaks** are computed once by `wrap_text`. The ASS file uses `WrapStyle: 2` (no libass
  wrapping), so preview and export break lines in the same places.
- **Background box** is drawn as a second ASS layer (`BorderStyle 3`, invisible text) under the
  normal outlined text, so the box and the outline can be used together.
- **Shaping**: the overlay builds text from `QTextLayout` glyph runs, so complex scripts
  (Bengali, Devanagari, Arabic) are shaped. libass shapes with HarfBuzz.
- **Burn-in** runs FFmpeg with its working directory set to the job's temp folder and passes the
  subtitle file as a bare name (`subtitles=captions.ass`). This avoids FFmpeg filter escaping of
  Windows paths.
- **Encoder**: `choose_video_encoder` uses NVENC only after a real 0.2 s test encode succeeds
  (the result is cached). An export that fails on NVENC is retried with libx264.
- **Transcription device**: CUDA (float16) is tried first when CTranslate2 sees a CUDA device or
  when CUDA is forced. A load or decode failure falls back to CPU int8. One model is cached, and
  transcriptions run one at a time.
- **AI correction** sends `{id, text}` per caption in batches of 60 and asks for JSON. Answers are
  validated: unknown ids, empty or implausibly long text, and unchanged text are dropped.
  Suggestions are stored on the track as `pending_corrections` until the user reviews them.

`core/`, `storage/` and `jobs/` never import Qt, so they can be tested without a display and
reused by a future command-line or batch runner.

## Auto reframe pipeline (Phase 3)

```
Analyse (job reframe_analyze)
  ffmpeg -vf fps=N:round=up,scale=… -f rawvideo bgr24  ──pipe──▶ core.process.run_frames (one frame in RAM)
      └─▶ reframe.detection.DetectorSet (YuNet faces | Haar fallback; HOG persons when no face)
          └─▶ reframe.tracker.IouTracker ──▶ TrackingAnalysis (normalised boxes, stored in the project)
Settings change (GUI thread, no decoding)
  reframe.framing.compute_crop_path(analysis, settings) ──▶ CropPath (10 keyframes/s, source pixels)
      desired position → zero-lag EMA → camera (dead zone, speed limit, transitions, keep-in-frame) → clamp
Export (job reframe_export; payload = crop keyframes + output size + ASS text snapshot)
  reframe.export.sendcmd_script ──▶ reframe.cmd ; captions.ass      (job temp folder, plain names)
  ffmpeg -vf sendcmd=f=reframe.cmd,crop=…,scale=W:H,setsar=1[,subtitles=captions.ass]
      └─ jobs.encode.encode_with_fallback (NVENC after test encode, else libx264; .part → rename)
```

The Auto Reframe page draws both previews from the frame the player has already decoded; the
**Exact Frame Preview** job renders one frame through the same FFmpeg filters as the export.

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

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
   QPainter overlay (live)    build_ass ──► captions.ass ──ffmpeg ass=…:shaping=complex (libass)──► MP4
                                            (frame preview job renders 1 PNG the same way)
```

- **Sizes** are in pixels for a 1080-pixel-tall frame and scaled to the video height. The ASS file
  uses `PlayResX/Y` = the video's display size, so libass and the overlay agree.
- **Line breaks** are computed once by `wrap_text`. The ASS file uses `WrapStyle: 2` (no libass
  wrapping), so preview and export break lines in the same places.
- **Background box** is drawn as a second ASS layer (`BorderStyle 3`, invisible text) under the
  normal outlined text, so the box and the outline can be used together.
- **Shaping**: the overlay builds text from `QTextLayout` glyph runs, so complex scripts
  (Bengali, Devanagari, Arabic) are shaped. libass shapes with HarfBuzz: captions are burned in
  with the `ass` filter and `shaping=complex` (the `subtitles` filter uses simple shaping, which
  breaks Bengali conjuncts and vowel signs).
- **Burn-in** runs FFmpeg with its working directory set to the job's temp folder and passes the
  subtitle file as a bare name (`ass=captions.ass:shaping=complex`). This avoids FFmpeg filter escaping of
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
  ffmpeg -vf sendcmd=f=reframe.cmd,crop=…,scale=W:H,setsar=1[,ass=captions.ass:shaping=complex]
      └─ jobs.encode.encode_with_fallback (NVENC after test encode, else libx264; .part → rename)
```

Analysis features follow the tracking mode (`ReframeSettings.analysis_features()`):
`faces`/`persons` → detector + tracker (with size/appearance gates and offline re-linking,
`reframe/tracker.py`), `mouth` → `reframe/mouth.py` (YuNet landmarks, 15 frames/s), `speech` →
`reframe/speech.py` (FFmpeg audio aligned with `aresample=first_pts=0`, Silero VAD bundled in
faster-whisper, 30 s chunks). `reframe/speaker.py` turns mouth movement + speech into per-moment
speaker evidence, and `framing._Resolver` decides whom to follow per mode (with confirmation,
hold and fallback) before the shared smoothing/camera stage. `model.reanalysis_reasons()` refuses
analyses that lack what the mode needs.

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

## Packaged application (Windows)

- `packaging/windows/ai_video_studio.spec` builds two EXEs that share one `_internal` folder.
  `packaging/windows/launcher.py` calls `ai_video_studio.__main__._run`, so the source and packaged
  apps start up the same way.
- `core/paths.py`:
  - `is_frozen()`, `exe_dir()` (folder of the EXE), `app_root()` (`_internal` when frozen, for
    bundled resources);
  - `data_dir()`: `AVS_DATA_DIR`, then `portable.txt` → `<exe>/UserData`, then
    `%LOCALAPPDATA%\AIVideoStudio`. User data is never written to the program folder.
- `core/ffmpeg_tools.find_tool`: the Settings folder, then `exe_dir()/bin`, `app_root()/bin`, then
  `PATH`.
- `core/download.py`: a stdlib downloader with `.part` files, Range resume, retries and size,
  SHA-256 and git-sha1 checks.
- `captions/models.py`: the Whisper model catalogue, status, downloads and removal, under
  `models_dir()/whisper`. Downloads run as the `model_download` job and never start without user
  confirmation.
- `tools/self_test.py` (`--self-test`) runs the real pipeline inside the installed program and
  writes a JSON report. The Windows build workflow and `packaging/windows/test_packaged.ps1`
  require its checks to pass.

## Edited exports (Phases 4–6)

- `render/output.py`: `OutputSettings`, covering codec (H.264/H.265), encoder preference
  (auto/GPU/CPU), quality preset or CRF, target bitrate, frame rate and audio mode.
  `core/ffmpeg_commands.choose_video_encoder` uses NVENC only after a 1-frame test encode, per
  encoder.
- `render/pipeline.py` builds one `-filter_complex` graph:
  - `split`/`trim`/`setpts` and `asplit`/`atrim`/`asetpts` for each kept range, then `concat`;
  - a video stage on the joined timeline;
  - colour grade / video filter (`render/color.py`: `curves`, `colorchannelmixer`, `eq`,
    `vignette`, `unsharp`, soft glow), after the framing;
  - `fps`, then the captions (`ass`, with caption animations from `captions/animation.py`) on
    the final canvas;
  - cut points snapped to the frame grid;
  - graphs longer than 6000 characters go to `-filter_complex_script` (Windows command-line
    limit).
- `render/stages.py` provides three video stages:
  - `layout` (Resize & Crop);
  - `reframe` (moving crop via `sendcmd`, with keyframes mapped from the source timeline to the
    edited timeline);
  - `scale` (previews).
- `render/layout.py`: `compute_layout` maps a source rectangle to a canvas rectangle, in even
  integers. It is the single geometry used by the canvas preview and `layout_filter`.
- `jobs/render_jobs.py` contains the export jobs:
  - `render_video` (heavy): `.part` output, never the source, existing files replaced only when
    the payload says so, and an in-process registry so two jobs never write one file. Batch
    Auto Reframe items run `reframe_jobs.run_tracking_analysis` first (job stage "analyzing").
  - `render_frame_preview`: one frame through the same graph.
  - `smartcut_analyze`: `astats` levels.
- `smartcut/`: `levels.py` parses `ametadata` output and stores levels compactly in the
  project. `model.py` holds detection, the segment partition, edits, undo/redo and the applied
  keep ranges.
- `render/batch.py`: `RenderPreset` (built-in and user presets in `render_presets`), `Batch`
  (saved in `batches`) and `build_payload`. `render/naming.py` handles templates, Windows-safe
  names and `plan_outputs` (conflict rules).
- `jobs/manager.py`:
  - heavy job kinds (renders) have their own limit (`max_concurrent_renders`, default 1), and the
    dispatcher skips waiting heavy jobs so light ones still start;
  - the `held` state pauses individual queued jobs (batch pause);
  - `JobContext.set_stage` reports what a job is doing.

## AI providers (Phase 7)

- `ai/profiles.py`: `ProviderProfile` (no secrets) and `ProviderStore` (table `ai_providers`, in
  priority order). The first start imports the earlier AI settings.
- `storage/secrets.profile_key`: the profile's environment variable first, then the credential
  store (account = profile id).
- `ai/client.py`: Gemini `generateContent`, OpenAI or compatible `chat/completions`, `models`
  listing for connection tests, usage parsing and key scrubbing.
- `ai/router.py`: the capability-aware plan, bounded retries with backoff, fallback rules,
  cancellation, the local rate limiter and usage figures. `ai/usage.py` holds the request log
  (`ai_requests`, no content).
- Jobs: `caption_correction` (payload snapshot of the profiles; old single-provider payloads still
  run), `ai_test`, `ai_titles`. Keys are looked up when a job runs and are never put in a payload.

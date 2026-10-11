# Changelog

All notable changes are listed here. Versions follow semantic versioning.

## [0.7.0] - Unreleased

Phases 4 to 7: Smart Cut, Resize & Crop, Bulk Processing and the AI provider manager.

### Added — Phase 4: Smart Cut
- **Smart Cut page**, replacing the placeholder:
  - analyse the audio, with progress and cancel;
  - silence settings (threshold, minimum silence, padding before and after speech, minimum
    kept sound, merge gap), plus Suggest Threshold;
  - a timeline with levels, segments, threshold and playhead;
  - a segment table with Keep/Remove, editable boundaries, select all/none, and undo/redo;
  - Preview Segment, and "Play edited result" (skips removed parts);
  - Apply Cuts and Reset Analysis;
  - original, proposed and exported durations;
  - Export Preview (360p), Export Video and Export SRT (edited timeline);
  - export status with Cancel, Retry and Open Output Folder.
- **Audio analysis job** (`smartcut_analyze`): FFmpeg `astats` levels in 25 ms windows, stored
  compactly in the project. Handles videos without audio, audio that starts late or is shorter
  than the video, multiple channels and digital silence.
- **Edited export job** (`render_video`), shared with Resize & Crop and Bulk Processing:
  - frame-accurate `trim`/`concat` cuts with frame-snapped boundaries;
  - H.264 or H.265 (CPU or NVENC) and constant quality or target bitrate;
  - output frame rate; AAC audio, or copying it unchanged when nothing is cut;
  - optionally all audio tracks;
  - `.part` output files and duplicate-output protection.
- **Captions on the edited timeline**: burn-in and SRT export use remapped captions. Remapping
  never creates negative times or new overlaps, and the caption track keeps its own timing.
- Settings → Export: default settings for edited exports (the last used settings are remembered).
- Export Queue: Open Output Folder; labels for every job type.

### Changed
- Project format version 5 (adds the optional `smart_cut` and `resize` entries per video).
  Older projects open unchanged.
- `EncoderSettings` supports libx265, hevc_nvenc and target bitrates. NVENC availability is
  tested per encoder.

## [0.3.2]

Windows packaging: the app can be installed and run without Python. No new editing features.

### Added
- **Windows program** built with PyInstaller (one-folder):
  - `AI-Video-Studio.exe` (no console window) and `AI-Video-Studio-Console.exe` (diagnostics),
    both with the app icon and version information.
  - Bundled: Qt Multimedia with its FFmpeg plugin, OpenCV, faster-whisper/CTranslate2,
    onnxruntime (Silero VAD) and the YuNet face model.
  - **FFmpeg 7.1.1 essentials (Gyan, GPL v3)** in `bin\`, downloaded at build time against a pinned
    SHA-256, with its licence and source information in `licenses\ffmpeg\`.
- **Inno Setup installer**:
  - per-user by default, without admin rights; you can choose the folder;
  - Start Menu entry and Tools folder (diagnostic console, environment check, self-test), optional
    desktop icon, uninstall entry;
  - upgrades in place;
  - uninstall keeps settings, logs and models unless you choose to delete them (default No; a
    silent uninstall never deletes them).
- **Portable ZIP**. A `portable.txt` file next to the EXE keeps all data in `UserData\` beside it.
- **Whisper model manager**:
  - Settings → Speech recognition models: download as a background job with progress, delete,
    open folder.
  - Generate Captions asks before downloading a missing model, showing its size and destination.
  - Downloads are checked against the sizes and SHA-256/git checksums published by Hugging Face,
    pinned to one revision, resumed after interruption, retried with backoff, and checked for free
    disk space first.
  - Models are stored in `%LOCALAPPDATA%\AIVideoStudio\models\whisper\`. Models already in the
    Hugging Face cache are reused.
- **Command-line tools** in the packaged app: `--check-env`, `--self-test` (16 functional checks
  inside the installed program, JSON report), `--download-model NAME`, `--help`.
- **Build system**:
  - `scripts\windows\build.bat` and `packaging/build.py`;
  - locked dependency versions in `packaging/windows/constraints.txt`;
  - outputs: `dist\AI-Video-Studio\`, ZIP, setup EXE, `build-info.json`, `SHA256SUMS.txt`.
- **Windows build workflow** (`.github/workflows/build-windows.yml`):
  - runs the tests, then builds;
  - tests the packaged EXEs with no Python or FFmpeg on `PATH`: GUI start-up, console check,
    model download, self-test, settings persistence, missing FFmpeg, portable ZIP, and installer
    install, upgrade and uninstall;
  - uploads the artifacts; tags `v*` publish a GitHub pre-release.
  - `packaging/windows/test_packaged.ps1` runs the same tests on your PC.

### Changed
- FFmpeg is searched for in this order: the Settings folder, then `bin\` next to the EXE, then
  `PATH`. The "not found" message explains each option.
- The bundled YuNet model is used before any download, so auto reframe works offline in the
  packaged app.

### Not verified
- GPU transcription and NVENC exports in the packaged app (no GPU on the CI runners). The standard
  build is CPU-only; `build.py --gpu` (bundles CUDA runtime libraries) is experimental and untested.
- The installer's interactive pages, SmartScreen and the look on a real Windows 10/11 desktop.

## [0.3.1]

Phase 3 upgrade: selectable tracking modes, active-speaker framing and tracking fixes.

### Added
- **Tracking modes**, selectable in Settings → Tracking (default for new videos, kept after restart)
  and per video on the Auto Reframe page (saved in the project):
  - Auto Face Tracking
  - Face + Speech Audio (Active Speaker)
  - Speech-Audio-Driven
  - Manual Subject Tracking
  - Automatic Visible Subject
  - Center Crop

  Each mode shows only the options it uses.
- **Active speaker**: mouth-region movement per face (from YuNet mouth landmarks, compensated for
  head movement) is matched with speech activity and loudness from the audio. The followed speaker
  changes only after the evidence lasts for the confirmation time. Silence never causes a switch,
  and off-screen or overlapping speech is treated as uncertain.
- **Speech activity** with the Silero VAD bundled in faster-whisper (CPU, offline, no new
  dependency). Audio is aligned to the video's time zero, even when the audio stream starts late.
- **Fallback** options (centre, hold, frame everyone visible), a maximum upscale factor, a tracking
  confidence threshold, a speech threshold, the speaker-switch confirmation time and a speaker
  hold time.
- **Reanalysis detection**: analyses record what they contain. A mode that needs more shows
  "Reanalysis required" and never uses the stored results; those results are kept for the modes
  that can use them.
- **Status at the playhead**: mode, subject, detections, speech, speaker evidence, state, crop
  size and upscale factor.
- Tests:
  - every mode and the active-speaker scenarios, on synthetic data
  - real VAD and audio alignment
  - real detector + VAD on generated audio-visual clips (CI)
  - Settings persistence across a restart, the mode in saved projects, and the reanalysis banner

### Fixed (root causes found in the v0.3.0 implementation)
- **Low "coverage" and "unreliable tracking" warnings for subjects who are simply not in every
  part of the video**: coverage was measured against the whole video. Reliability is now the
  detection rate *while on screen*; partial presence is a note, not a warning.
- **One person split into several IDs after an occlusion**: tracks closed after 1.5 s without a
  detection and were never re-linked. Track pieces are now joined offline when position, size and
  colours agree and the choice is unambiguous.
- **Crop jumping to an unintended foreground person**: the automatic choice was dominated by face
  size, so a large face near the camera won, and size changes were not checked when matching
  detections. The choice now uses screen time and detection reliability. Matches are refused on
  large size jumps or clearly different colours.
- **Excessive upscaling**: zoom had no limit. A maximum upscale now limits it, with a warning when
  the source is too small for the output.
- **Caption-safe framing conflicts**: the caption area was a fixed 25 % unrelated to the real
  captions, and it was enforced as a hard clamp after smoothing, which caused jumps. It is now
  derived from the caption style and applied as a smoothed preference, with a warning when the
  crop cannot avoid the captions.
- **Framing**: the protected area includes the chin and neck (start of the upper body). The
  default face height is 38 %.
- **Intermittent crash at exit** (`Fatal Python error: Aborted` on Windows CI since v0.2.1;
  segmentation fault on Linux): a core dump showed PySide destroying left-over Qt objects at
  interpreter exit (`QObject::~QObject` in `destroyQCoreApplication`). The window's job bridge and
  project controller had no Qt parent. They are now owned by the window, the app destroys the window
  before shutdown, and the bridge stops receiving job events when it is destroyed. Linux full test
  runs went from 5 of 6 crashing to 8 of 8 clean.

### Changed
- Project format version 4: the tracking mode replaces v0.3.0's track/center setting, which is
  converted when older projects are opened.
- The Haar fallback keeps its own detection gate (its score is not comparable with YuNet's).

## [0.3.0]

Phase 3: face/person tracking and AI auto reframe.

### Added
- **Auto Reframe page** (replaces the "Face Tracking" placeholder):
  - Background analysis job: FFmpeg decodes sampled, downscaled frames (one in memory at a time);
    OpenCV detects faces (YuNet DNN; Haar cascade fallback when the model cannot be downloaded)
    and, optionally, full-body people (HOG) in frames without faces. Progress and cancel.
  - Tracker with stable subject IDs, confidence, occlusion bridging and false-positive filtering.
  - Subject choice: automatic, a chosen subject from the start, or a switch from the playhead.
    Reappearance near the last position continues the same subject; ambiguous cases do not.
  - Presets: original, 16:9, 9:16, 1:1, 4:5, with selectable output sizes.
  - Virtual camera: zero-lag smoothing, dead zone with hysteresis, speed limit, hold-then-recentre
    on loss, eased transitions on subject changes; the face is always kept in the crop.
  - Manual framing (zoom, padding, subject height, offsets, reset) and an explicit Center crop mode.
  - Caption-safe area (bottom/top) and optional caption burn-in; captions themselves are untouched.
  - Source preview with crop, boxes, IDs and caption area; reframed output preview; FFmpeg
    exact-frame preview.
  - Export with a crop that moves per frame (`sendcmd` → `crop` → `scale` → `subtitles`),
    keeping frame rate and audio sync; NVENC only after a successful test encode, else libx264.
  - Warnings for no subjects, low coverage or confidence, damaged files, changed source files and
    the Haar fallback; export asks for confirmation while a warning is shown.
- Dependency: `opencv-python-headless>=4.10,<5` (CPU; no CUDA in pip builds).
- `check_env` reports OpenCV, the face detection device and whether the YuNet model is present.
- Tests: crop geometry for all presets, bounds, smoothing, dead zone, speed limit, loss recovery,
  subject changes, caption-safe framing, tracker IDs, detector fallbacks, model download checks,
  sendcmd/filter generation, persistence, real-FFmpeg analysis/export with pixel checks,
  cancellation, damaged files, GUI tests, and real YuNet/Haar detection on a public-domain photo
  (CI downloads the model and photo; both are hash-checked).

### Changed
- Project file format version 3 (adds optional per-video `reframe` data). Version 1 and 2 files
  still open.
- Burn-in export and reframe export share one encode step (`jobs/encode.py`); behaviour unchanged.

### Fixed
- **Possible freeze when a video was loaded into or removed from a preview** (all pages): the
  preview called `pause()` from inside `player.stop()`, which could deadlock Qt's FFmpeg media
  backend. Found by repeated GUI test runs; the call is now deferred.

## [0.2.1]

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

### Changed
- Bengali guidance is now based on measurements of real speech: the Generate Captions dialog says
  that tiny–medium are not usable for Bengali, and that large-v3 / large-v3-turbo need proofreading
  (about 4 in 10 characters wrong). This replaces the earlier, incorrect "small or larger" advice.
- README verification table separates synthetic-audio tests from real-speech results. Mocked AI
  providers, the untested GPU path and offscreen-only UI checks are labelled as such.

### Added (diagnostics)
- Manual workflow `whisper-diagnostics.yml`:
  - real-model transcription of the fixtures (tiny/small/medium, VAD on/off, auto-detect)
  - real Bengali speech CER from Google FLEURS (small, medium, large-v3, large-v3-turbo)
  - a Windows probe for the intermittent exit abort

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

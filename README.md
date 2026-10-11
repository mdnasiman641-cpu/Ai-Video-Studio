# AI Video Studio

A Windows desktop video editor (Python + PySide6 + FFmpeg) built for 1080p
short-form work: captions, face-tracked reframing, silence removal, resizing and
batch export. It is being built in phases. **Phases 1 (foundation), 2 (captions)
and 3 (face tracking and auto reframe) are done.** Later features are listed in the
app as "planned" and are not presented as working.

## Status

| Phase | Scope | Status |
|------|-------|--------|
| 0 | Repository audit and plan | Done ([documentation/ROADMAP.md](documentation/ROADMAP.md)) |
| 1 | Foundation: UI shell, import, metadata, preview, projects, SQLite, jobs, settings, logging | **Done** |
| 2 | Caption generation, editing, styling, SRT, burn-in, optional AI correction | **Done**: implemented and tested; see [Verification status](#verification-status) for what is and is not verified (Bengali quality, GPU, real AI providers) |
| 3 | Face/person tracking and AI auto reframe | **Done**: implemented and tested with real FFmpeg and real OpenCV detection; GPU/NVENC and your own footage not yet verified (see [Verification status](#verification-status)) |
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
  - **Bengali:** only large-v3 / large-v3-turbo produced Bengali text in tests, with about 4 in 10
    characters wrong. Smaller models are not usable for Bengali. See
    [Bengali: synthetic tests vs real speech](#bengali-synthetic-tests-vs-real-speech).
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

### What works now (Phase 3: AI auto reframe)

Open the **Auto Reframe** page, pick a video and click **Analyse Video**.

- **Detection** (OpenCV, runs on the CPU):
  - Faces: the **YuNet** DNN face detector. Its 230 KB model is downloaded automatically on the
    first analysis and checked against a SHA-256 hash. Without internet access, OpenCV's bundled
    **Haar cascade** is used instead (frontal faces only, less accurate) and a warning says so.
  - People: OpenCV's **HOG** person detector looks for full bodies in frames where no face is
    found (optional, slower). It finds upright, mostly full-body people that are reasonably large.
  - FFmpeg decodes frames at a reduced size (longer side 960 px) and a configurable rate
    (default 5 frames per second of video), one frame in memory at a time. On the Ryzen 7 7700
    this should be much faster than real time (measured here: 30 frames in under 1 s on a
    cloud CPU); it has **not been measured on your PC**.
- **Tracking**: every subject gets a stable ID (#1, #2, …) with a confidence value. Boxes are
  matched by overlap and predicted motion, so two people next to each other keep their IDs. Short
  gaps (occlusion, a missed detection) keep the ID. One-frame false detections are dropped.
- **Subject selection**: *Automatic* follows the best visible subject (faces preferred, then
  size, confidence and screen time). You can pick a subject to follow **from the start**, or switch
  to another subject **from the playhead**. Double-click a subject to jump to where it appears.
  When the followed person disappears and one new track appears near the same place within 3 s,
  it is treated as the same person. With two candidates it does not guess.
- **Formats**: original aspect ratio, 16:9, 9:16 (Reels, Shorts, TikTok), 1:1 and 4:5, with output
  sizes such as 1080×1920, 720×1280, 1080×1080, 1080×1350 and 1920×1080. The crop always has the
  target aspect ratio (no stretching) and stays inside the frame.
- **Camera motion**: smoothing (run forwards and backwards, so it adds no delay), maximum speed,
  a dead zone that ignores small movements while the camera is still, a hold time when the
  subject is lost (then an eased return to the centre), and eased transitions when the subject
  changes or reappears. The face is always kept inside the crop, even if that means moving faster
  than the speed limit.
- **Manual framing**: zoom, padding around the face, subject height in the frame, horizontal and
  vertical offset, and **Reset to Automatic Framing**. **Center crop (no tracking)** is a separate,
  explicit mode.
- **Caption-safe framing**: reserve the bottom or top part of the output for captions; the face is
  kept out of that area where the crop allows it. Captions can be burned in. Caption text, timing,
  style and SRT files are not changed.
- **Preview**: the source video with the crop rectangle, the detected boxes and IDs (the followed
  subject in green) and the caption area; next to it the reframed output with captions, drawn from
  the frame already decoded by the player. **Exact Frame Preview** renders the current frame with
  FFmpeg exactly as the export will.
- **Export**: the crop moves frame by frame. A `sendcmd` script generated from the tracking path
  drives FFmpeg's `crop` filter, followed by scaling and optional subtitle burn-in in one filter
  graph. Timestamps are kept, so the frame rate and audio sync match the source. AAC audio is
  copied. The same encoder logic as caption export is used: NVENC only when a test encode succeeds,
  otherwise libx264, and libx264 again if NVENC fails mid-export. Output goes to `*.part.mp4` first;
  cancel removes it. The source video is never modified.
- **Warnings instead of silent bad results**: no faces/people found, subject visible in under half
  the video, low confidence, decoding stopped early (damaged file), video changed since analysis,
  Haar fallback in use. Exporting with an open warning asks for confirmation first.
- Tracking results and settings are saved in the project (format version 3; version 1 and 2 files
  still open), so reopening a project does not re-analyse.

### Verify an exported reframed video

1. Play it next to the **Exact Frame Preview** at a few positions; they should match.
2. Check the size, frame rate and audio, for example:
   ```bat
   ffprobe -v error -show_entries stream=codec_type,width,height,avg_frame_rate -of compact output.mp4
   ```
   The video line should show the chosen output size and the same frame rate as the source, and
   there should be an audio line if the source had audio.
3. Scrub through moments where people move, turn away, leave or enter. The followed person should
   stay in frame, the camera should not jitter or jump, and captions should not cover the face.

### Known limitations (auto reframe)

- **Detection runs on the CPU only.** The pip OpenCV builds have no CUDA, so the RTX 4070 is not
  used for detection (it can still be used for transcription and, if verified, NVENC encoding).
- **Profile and turned-away faces**: YuNet finds frontal and moderately turned faces; a face seen
  from the side or from behind is often lost. Turning on full-body detection helps only when no
  other face is visible, because the person detector runs only on frames without any face.
- **Full-body detection (HOG)** is basic: upright, mostly full people at least about a quarter of
  the frame height. Seated, cropped or small people are often missed. Its accuracy has not been
  measured on real footage.
- **Identity is position-based.** There is no face recognition: after someone leaves and comes
  back, they get a new ID unless they reappear near the same place within 3 seconds. Use
  **Follow From Playhead** to pick them again.
- **Fast motion** between analysed frames is interpolated; raise the analysis rate for sports or
  quick movement.
- The crop has a fixed size per export (no automatic zoom). A 9:16 crop of a 1080p video is
  608×1080 and is upscaled to 1080×1920, which softens the image; the output info line shows the
  scale factor.
- Caption sizes follow the caption style and scale with the output height, as for any export. For
  vertical output you may want a smaller font or fewer characters per line (Captions → Style).

## Verification status

Automated checks run on every push in GitHub Actions ([`.github/workflows/ci.yml`](.github/workflows/ci.yml))
on `windows-latest` (Windows Server, Python 3.12, Gyan FFmpeg via Chocolatey) and `ubuntu-latest`.
GitHub's runners have **no NVIDIA GPU**, and nothing has been tested on a Windows 10/11 desktop yet.

| Area | How it is checked | Result |
|------|-------------------|--------|
| FFmpeg / FFprobe detection | `check_env`, real FFprobe tests | Passed on Windows and Linux |
| App start-up (PySide6) | `--smoke-test` with the native Windows platform, under `pythonw.exe`, and via `run.bat` | Passed on Windows |
| `setup.bat`, `run.bat`, `run_tests.bat` | Executed on `windows-latest` | Passed |
| Project save / reopen, settings, job queue | Unit + GUI tests | Passed on Windows and Linux |
| Caption editing, SRT import/export, styles, presets | Unit + GUI tests (offscreen) | Passed on Windows and Linux |
| Live caption overlay | Pixel test on an offscreen image | Passed; **not checked by eye on a real screen** |
| Subtitle burn-in (libass) | Real FFmpeg export, pixel comparison | Passed on Windows and Linux |
| Export cancel cleanup | Real FFmpeg export cancelled mid-way | Passed on Windows and Linux (after two Windows fixes, see CHANGELOG) |
| Windows Credential Manager (API keys) | Real store / read / delete | Passed on Windows |
| **Real Whisper inference, English** | faster-whisper tiny (CI) and small/medium (diagnostics), CPU, **synthetic** espeak-ng English, full video → caption job | Passed: word-perfect with small and medium. **Real English recordings not tested** |
| Language auto-detect, forced CUDA → CPU fallback | Real model on a machine without a GPU | Passed on Windows and Linux |
| **Real Whisper, Bengali, synthetic voice** | CI test with tiny + small | **Expected failure (xfail)**: the espeak-ng Bengali voice is not recognised as Bengali by any model. See below |
| **Real Whisper, Bengali, real human speech** | Manual diagnostics, Google FLEURS `bn_in`, 5 sentences, CPU | tiny/small/medium: **unusable** (CER 1.000). large-v3: **CER 0.379**, large-v3-turbo: **CER 0.465**. Draft quality only |
| Large models in the normal app flow | large-v3 / large-v3-turbo through the transcriber (diagnostics) | Ran on CPU; **GPU not tested**, and these models are not part of the regular CI |
| Optional AI correction (Gemini / OpenAI) | Request/response handling with **mocked** HTTP | **Not tested against the real APIs** (no API keys in CI) |
| NVIDIA CUDA transcription, NVENC encoding | Detection logic + CPU fallback only | **Not verified** (no GPU available); NVENC is listed in FFmpeg but the test encode correctly fails on the runners |
| Cut sync (`remap_to_cuts`) | Unit tests | Passed; no cutting UI until Phase 4 |
| **Phase 3 crop maths** (all presets, bounds, smoothing, dead zone, speed limit, loss/hold, subject changes, reappearance, caption-safe area) | Unit tests on synthetic tracks | Passed |
| Tracker IDs (two people crossing, occlusion, false positives, fast motion) | Unit tests on synthetic detections | Passed |
| **Real face detection** (YuNet, Haar fallback) | Public-domain NASA photo moving through a generated video; one and two faces | Passed on Linux in this environment (YuNet: face in 30 of 30 frames, confidence 0.93; two people kept apart). CI runs it on Windows and Linux |
| HOG person detection | Runs in the pipeline; no test footage of full-body people | **Accuracy not measured** |
| Analysis timing, time-varying crop export, scaling, captions, audio, frame rate | Real FFmpeg: moving marker, colour-threshold stand-in detector, pixel checks on the exported MP4 | Passed |
| Reframe export / analysis cancel, corrupt and missing files | Real FFmpeg | Passed |
| Auto Reframe page | GUI tests (offscreen) | Passed; **not checked by eye on a real screen** |
| Detection speed on your Ryzen 7 7700, real talking-head footage | — | **Not measured / not tested** |
| GPU for detection | — | Not used: pip OpenCV has no CUDA. Detection is CPU-only by design |

### Bengali: synthetic tests vs real speech

The repository has two kinds of Bengali evidence, and they measure different things:

1. **The CI test uses a synthetic voice** (`tests/fixtures/speech_bn.flac`, made with espeak-ng).
   It checks that the Bengali language setting reaches Whisper and that the pipeline returns text.
   It is marked as an expected failure because no Whisper model writes Bengali script for this
   robotic voice:
   - Auto-detect classifies it as Latin (small and medium, p≈0.25).
   - Forcing `bn` gives romanised text (tiny), Kannada or Latin with diacritics (small), or Khmer and
     Georgian gibberish (medium).

   The same pipeline transcribes the synthetic **English** clip word-perfectly, so this shows the
   synthetic Bengali voice is unintelligible to Whisper, not that the app is broken. It says
   nothing about real-world Bengali quality.
2. **Real human Bengali speech** was measured with the manual
   [Whisper diagnostics workflow](.github/workflows/whisper-diagnostics.yml). It uses Google FLEURS
   `bn_in` (CC-BY-4.0, downloaded at run time, 5 different sentences, CPU, language = `bn`) and
   reports the character error rate (CER) against the reference transcripts:

   | Model | CER | What the output looked like |
   |-------|-----|-----------------------------|
   | tiny / small / medium | 1.000 | Telugu, Kannada, Devanagari, Georgian or Latin script; invented English phrases |
   | large-v3 | 0.379 | Bengali script with mostly correct words; often **stopped before the end** of the sentence |
   | large-v3-turbo | 0.465 | Bengali script with mostly correct words; sometimes **repeated nonsense** at the end |

**Conclusion:** use large-v3 or large-v3-turbo for Bengali, and treat the result as a draft that
needs careful proofreading in the caption editor. Five read-aloud sentences on a CPU are a small
sample. Your own recordings (accent, noise, music) may do better or worse, so check quality on
your own videos. GPU inference was not tested and should give the same text, only faster.

## Requirements

- Windows 10/11 64-bit
- Python 3.12 (64-bit) from python.org. This is the version tested in CI; 3.10–3.13 should work but are untested.
- FFmpeg + FFprobe with libass, for example `winget install Gyan.FFmpeg`. You can also set the
  folder that contains `ffmpeg.exe` in Settings, or put the binaries in `bin\` next to the app.
- Internet access the first time you transcribe, to download the Whisper model from Hugging Face
  (about 75 MB for tiny, about 480 MB for small, about 3 GB for large-v3). Alternatively, download a
  faster-whisper (CTranslate2) model yourself and choose its folder in Settings.
- For Bengali, use **large-v3** or **large-v3-turbo** (about 3 GB / 1.6 GB). tiny, small and medium did
  not produce Bengali text in tests, and even the large models need proofreading (see above).
- Optional GPU transcription: `scripts\windows\setup.bat --gpu` installs the CUDA 12 cuBLAS and
  cuDNN 9 runtime wheels. Not yet verified on real NVIDIA hardware.
- Auto reframe uses `opencv-python-headless` (installed by `setup.bat`). The first analysis downloads
  the YuNet face model (230 KB) from the OpenCV model zoo on GitHub; without internet the less
  accurate Haar cascade is used. Do not install the non-headless `opencv-python` package in the
  same environment; it bundles its own Qt libraries.
- Optional AI correction: a Gemini or OpenAI API key, set through the `GEMINI_API_KEY` /
  `OPENAI_API_KEY` environment variables or stored from Settings in the Windows Credential Manager.

## Setup and run on your Windows PC

1. Install **Python 3.12 (64-bit)** from https://www.python.org/downloads/windows/ and tick
   "Add python.exe to PATH" during setup.
2. Install **Git** (`winget install Git.Git`) and **FFmpeg** (`winget install Gyan.FFmpeg`).
   Close and reopen the terminal afterwards so PATH is updated.
3. Get the code:
   ```bat
   git clone https://github.com/mdnasiman641-cpu/Ai-Video-Studio.git
   cd Ai-Video-Studio
   ```
4. Install dependencies and check the environment. For GPU speech recognition on your RTX 4070, add `--gpu`:
   ```bat
   scripts\windows\setup.bat --gpu
   ```
   The last lines must say `Environment OK`. Check that the `CUDA devices visible to CTranslate2`
   line shows `1`; if it shows `0`, transcription will use the CPU. The `OpenCV` line must be OK;
   `Face detection device: CPU` is expected (face detection does not use the GPU).
5. Start the app: `scripts\windows\run.bat` (or `scripts\windows\run.bat --console` to watch live logs).
6. Optional: run the tests with `scripts\windows\run_tests.bat`. To include the real-Whisper tests:
   ```bat
   .venv\Scripts\python -c "from huggingface_hub import snapshot_download as d; d('Systran/faster-whisper-tiny', local_dir='.cache/whisper-tiny')"
   set AVS_TEST_WHISPER_MODEL_DIR=%CD%\.cache\whisper-tiny
   scripts\windows\run_tests.bat
   ```
   To include the real face detection tests (downloads the 230 KB YuNet model and a public-domain test photo):
   ```bat
   mkdir .cache\reframe-assets
   .venv\Scripts\python -c "from pathlib import Path; from ai_video_studio.reframe.detection import ensure_face_model as f; print(f(Path('.cache/reframe-assets')))"
   curl -L -o .cache\reframe-assets\astronaut.png https://raw.githubusercontent.com/scikit-image/scikit-image/v0.19.3/skimage/data/astronaut.png
   set AVS_TEST_REFRAME_ASSETS=%CD%\.cache\reframe-assets
   scripts\windows\run_tests.bat
   ```

If the app does not start, look for `%LOCALAPPDATA%\AIVideoStudio\startup_error.log` and
`%LOCALAPPDATA%\AIVideoStudio\logs\ai_video_studio.log`.

Other scripts:

```bat
scripts\windows\check_env.bat      :: re-check Python, PySide6, FFmpeg, Whisper, CUDA, NVENC
scripts\windows\setup.bat --dev    :: also install test tools
```

Manual equivalent:

```bat
py -3.12 -m venv .venv
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
| Whisper models, YuNet face model | `%LOCALAPPDATA%\AIVideoStudio\models\` |
| Tracking results, reframe settings | in the project file |
| Caption presets | in `studio.db` |
| API keys | environment variables or Windows Credential Manager (never in files or logs) |

Set `AVS_DATA_DIR` to use another data folder, for example for a portable install. Source videos
are never modified.

## Documentation

- [documentation/ARCHITECTURE.md](documentation/ARCHITECTURE.md): modules, threading and data flow
- [documentation/ROADMAP.md](documentation/ROADMAP.md): audit, phase plan, risks and dependency notes
- [documentation/TROUBLESHOOTING.md](documentation/TROUBLESHOOTING.md)
- [CHANGELOG.md](CHANGELOG.md)

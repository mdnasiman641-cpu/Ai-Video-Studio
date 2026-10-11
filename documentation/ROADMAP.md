# Roadmap, audit and risks

## Phase 0: repository audit (2026-10-10)

- `mdnasiman641-cpu/Ai-Video-Studio` was **empty**: no commits, branches or files. There was no
  existing implementation to keep or migrate.
- This session also had `mdnasiman641-cpu/APP`, which holds an unrelated Chrome extension
  (Facebook Bulk Reel Assistant). Nothing was taken from it and it was not modified.
- So Phase 1 builds the foundation from scratch, on branch `claude/bold-goldberg-nhol0w`.

## Technology decisions

| Concern | Choice | Notes |
|--------|--------|-------|
| UI | PySide6 6.7–6.12 | LGPL. Qt Multimedia uses an FFmpeg backend on Windows, so preview handles H.264/H.265. |
| Video I/O | FFmpeg/FFprobe CLI | Located via Settings, then `bin/` next to the app, then `PATH`. Not linked as a library. |
| Storage | SQLite (stdlib) + JSON project files | The DB holds per-user state. Projects stay portable. |
| Jobs | stdlib threads + SQLite persistence | No extra dependency. Phase 8 may move CPU-heavy Python work (tracking) to processes. |
| Tests | pytest + pytest-qt | GUI tests run offscreen. Sample videos are generated with FFmpeg at test time. |

Phase 1 adds no other dependencies. Planned additions, each checked for compatibility when its phase starts:

- Phase 2 (added): `faster-whisper` 1.1–1.x (CTranslate2 4.x; CUDA 12 cuBLAS + cuDNN 9 for GPU,
  optional `requirements-gpu.txt`; CPU int8 fallback) and `keyring` (Windows Credential Manager).
  Model weights download at first use to the data folder, never into the repo.
- Phase 3 (added): `opencv-python-headless` only (MediaPipe was evaluated and not used; see
  "Phase 3" below). Detection runs on the CPU. A CUDA alternative (e.g. an ONNX Runtime GPU face
  detector) will only be considered if measurements on real footage call for it.
- Phase 7: official `google-genai` and `openai` SDKs. Keys go in the Windows Credential Manager
  (via `keyring`) or the user DB, never in the repo.
- Phase 8: `psutil` for CPU/RAM. GPU usage via `nvidia-smi` or NVML when present.
- Phase 9: PyInstaller (one-folder build). FFmpeg is only bundled as an LGPL build with its
  license text included.

## Phase plan

1. **Foundation**: done (see README).
2. **Captions**: done (v0.2.0). Windows-readiness verification done in v0.2.1 (see below).
3. **Tracking/reframe**: done (v0.3.0). See "Phase 3" below.
4. to 9.: not started; listed under "Remaining phases".

## Phase 2 verification (v0.2.1, GitHub Actions `windows-latest` + `ubuntu-latest`)

Verified with real tools on Windows and Linux runners:
- FFmpeg/FFprobe detection and metadata
- PySide6 start-up: native Windows platform, `pythonw.exe`, `run.bat`
- `setup.bat`, `run.bat`, `run_tests.bat`
- Project save and reopen, caption editing, SRT import/export, styles and presets
- libass burn-in, checked by pixel comparison, and export cancel cleanup
- Windows Credential Manager
- Real faster-whisper English transcription, both directly and as a full video job
- Language auto-detect
- CUDA → CPU fallback on machines without a GPU

Bugs found by these checks and fixed: PyAV 19 broke all real transcription; `pythonw` had no
stdout/stderr; cancelled exports on Windows left FFmpeg running (launcher shims) and left partial
files behind.

Bengali investigation (manual [Whisper diagnostics workflow](../.github/workflows/whisper-diagnostics.yml)):
- **Synthetic espeak-ng Bengali** (the CI fixture) is not recognised as Bengali by tiny, small or
  medium. Auto-detect says Latin, and forcing `bn` gives romanised, Kannada, Khmer or Georgian output.
  The synthetic English clip is word-perfect on the same pipeline. The CI Bengali test therefore
  stays an expected failure; it checks the language path, not quality.
- **Real Bengali speech** (Google FLEURS `bn_in`, 5 sentences, CPU), character error rate:
  - tiny / small / medium: 1.000, unusable.
  - large-v3: 0.379. Correct script and mostly correct words, but output often stops before the
    end of the sentence.
  - large-v3-turbo: 0.465. Correct script and mostly correct words, but sometimes repeats text at
    the end.
- The Generate Captions dialog now tells users this when Bengali is selected.

Still unverified:
- **Bengali quality on the user's own recordings**, and whether tuning (VAD, beam size,
  `condition_on_previous_text`) reduces the truncation and repetition seen with the large models.
- **NVIDIA CUDA transcription and NVENC encoding.** No GPU is available in CI. Only the detection
  and the CPU fallback are tested.
- **Optional AI correction against the real Gemini/OpenAI APIs.** Only mocked HTTP is tested.
- **Real (non-synthetic) English recordings.**
- **A real Windows 10/11 desktop** and on-screen checks of the preview and overlay. CI runs
  offscreen on Windows Server.
- **A native abort at process exit**, seen in 2 of about 8 `windows-latest` runs. The test process
  printed `Fatal Python error: Aborted` after all 166 tests had passed. Each test group in its own
  process exits cleanly. A dedicated probe started 105 fresh processes on Windows covering every
  combination the app uses (Qt Multimedia, PyAV, CTranslate2, full faster-whisper transcription),
  and none aborted. The cause is not identified; so far it has only appeared at the end of the full
  pytest run. As a precaution, the app releases the Whisper model before interpreter shutdown.
  **Resolved in v0.3.1** (root cause found with a core dump on Linux, where the same crash
  reproduced in up to 5 of 6 full runs): the crash was in `QObject::~QObject` inside
  `PySide::destroyQCoreApplication`, i.e. PySide destroying left-over Qt objects in arbitrary
  order at interpreter exit. The left-overs were the main window's parentless `JobBridge` and
  `ProjectController` (one pair per window; also present in the app) and a dialog a test created
  without a parent. They are now parented to the window, the app destroys its window before
  shutting down, and GUI tests process deferred deletions. After the fix: 8 of 8 full runs exited
  cleanly on Linux. Windows CI is the remaining check.

## Phase 3: face tracking and auto reframe (v0.3.0)

Library choice: **OpenCV (`opencv-python-headless`) instead of MediaPipe.** The plan above named
MediaPipe; on inspection it was not chosen because:
- MediaPipe's current Tasks API also needs separately downloaded model files, so it saves nothing
  over OpenCV's YuNet model (230 KB, hash-checked).
- Its Windows wheels pin `protobuf`/`numpy` ranges and lag new Python releases, which risks
  conflicts with faster-whisper/CTranslate2.
- Its Python package runs on the CPU on Windows, like OpenCV. So it gives no GPU advantage.
- OpenCV headless brings the face detector (YuNet), a no-download fallback (Haar) and a person
  detector (HOG) in one dependency with no Qt libraries of its own.

Design:
- Frames are decoded by FFmpeg (`fps=N:round=up`, downscaled, raw BGR over a pipe), so analysis
  uses the same decoder, rotation handling and clock as export. `round=up` was needed: with the
  default rounding FFmpeg 6.1 picked frames about 2 source frames late (found by the real-FFmpeg
  timing test).
- Tracker: greedy IoU + predicted-centre matching per kind, tracks closed after 1.5 s unseen,
  one-frame tracks dropped, IDs renumbered by first appearance.
- Crop path (10 keyframes/s): desired position → forward-backward EMA with odd-reflection padding
  (no lag, no end effects) → camera simulation (dead zone with hysteresis, speed limit, eased
  transitions, hard keep-in-frame for the face) → clamped to the frame.
- Export: `sendcmd` script with a `crop x/y` command per output frame where the position changes,
  then `scale` and `subtitles` in one graph. Pixel tests confirm the crop follows the subject.

Phase 3 verification:
- Unit tests for geometry, smoothing, tracking, persistence and command generation.
- Real-FFmpeg tests: analysis timing, moving-crop export (pixel checks), captions, audio, frame
  rate, cancel and damaged files.
- Real OpenCV detection on a public-domain NASA photo moving through a generated video: YuNet found
  the face in 30 of 30 analysed frames (mean confidence 0.93) and kept two people apart; the Haar
  fallback also found the face, plus a short false track. CI runs these on Windows and Linux.

Still unverified (Phase 3):
- **Real footage** (talking heads, several people, movement, profile views, low light) and
  detection speed on the user's Ryzen 7 7700.
- **HOG person detection accuracy**: no full-body test footage.
- **NVENC encoding of reframed exports**: no GPU in CI (same as Phase 2).
- **A real Windows desktop** and on-screen checks of the Auto Reframe previews.
- Found and fixed while testing Phase 3: `VideoPreview` called `pause()` from inside
  `player.stop()` (via the `LoadedMedia` signal). This could deadlock Qt's FFmpeg backend when a
  clip was loaded or unloaded; it hung the GUI tests intermittently. The call is now deferred to
  the event loop.

## Phase 3 upgrade (v0.3.1): tracking modes and active speaker

Root causes of the problems seen on the user's Windows test were reproduced with controlled
inputs on the v0.3.0 code before anything was changed:
- **"Detected in only X %" / "unreliable"**: coverage was samples ÷ all analysed frames, so a
  person on screen for 40 % of the video, detected in every one of those frames, was reported as
  40 % and "unreliable".
- **Low coverage and lost subjects after occlusion**: a gap of more than 1.5 s closed the track,
  and the new piece got a new ID (2.4 s occlusion → two IDs). Nothing re-linked them.
- **Unintended foreground person**: the automatic score was coverage × confidence × √area. A large
  face near the camera ranked first and was taken over when the subject was lost. Matching
  checked position only (no size or appearance check).
- **Too tight / upscaled**: zoom had no upscale limit (2.67× at zoom 1.5 for 9:16 at 1080×1920).
- **Caption conflicts**: a fixed 25 % band unrelated to the caption style, enforced as a hard
  clamp after smoothing (jumps). A 9:16 crop of a landscape video cannot move vertically at all.

Fixes: detection rate while on screen; size and appearance gates; offline re-linking of track
pieces (unambiguous only); an automatic choice based on screen time and reliability; a maximum
upscale; a caption band derived from the caption style and applied before smoothing.

Tracking modes: Auto Face, Active Speaker (face + speech audio), Speech-Audio-Driven, Manual,
Automatic Visible Subject, Center Crop. The Settings default persists across restarts, and the
mode chosen per video is saved in the project (format 4).

Active speaker method (dependency evaluation):
- **Chosen:** Silero VAD (already installed with faster-whisper; onnxruntime CPU; no download) for
  speech activity, plus a measured mouth-region motion signal from YuNet landmarks, matched with
  the speech loudness. No new packages and fully offline.
- **Not chosen:**
  - Trained active-speaker models (TalkNet, Light-ASD, LoCoNet): they need PyTorch (about 2 GB for
    CUDA builds), model weights from research repositories and per-face audio-visual crops; there
    is no maintained Windows/ONNX package.
  - MediaPipe Face Mesh for lip landmarks: the same objections as in Phase 3.

  Either can be revisited if measurements on real footage show the heuristic is not good enough.
- **Speaker diarisation** (pyannote): needs PyTorch and a Hugging Face token, and would still not
  say which *visible* person speaks.

Verified:
- Synthetic unit scenarios for every mode.
- Real VAD on recorded speech, silence and a tone, and audio that starts 2 s late.
- Generated audio-visual clips through the real pipeline: speaker A, then B, correctly followed;
  silence held; off-screen voice not attributed.

Still unverified (Phase 3 upgrade):
- **Active speaker on real conversations** (podcasts, interviews, several people), including
  side views, masks or beards, and music with singing.
- **Analysis speed on the user's PC.** Measured here: Active Speaker analysis takes about a third
  of the clip's duration for 720p on a cloud CPU.
- Everything listed as unverified for Phase 3 above (real footage in general, NVENC, desktop
  screens).

## Windows packaging (v0.3.2, done ahead of Phase 9)

Built before Phases 4–8 so the app can be used without Python. The Phase 4–8 features are not
started.

**Decisions**
- **PyInstaller, one-folder.** Chosen over:
  - one-file, which would unpack about 600 MB to a temporary folder on every start, is slow, and is
    often flagged by antivirus software;
  - Nuitka, which is slower to build and has less tested support for PySide6, CTranslate2 and
    onnxruntime.

  Two EXEs share one folder: windowed and console.
- **Inno Setup 6, per-user by default.** No admin rights and a fixed AppId, so upgrades work.
  `_internal` is replaced on upgrade. User data is outside the program folder and is kept on
  uninstall unless the user agrees to delete it.
- **Portable ZIP** of the same folder, with an opt-in `portable.txt` for data next to the EXE.
- **FFmpeg bundled** (Gyan 7.1.1 essentials, GPL v3, pinned SHA-256) as separate executables, so
  the GPL program is not linked into the app, with licence, source information and a
  user-selectable replacement.
- **Whisper models not bundled.** They are downloaded on request with consent, verification and
  resume. YuNet (230 KB) is bundled.
- **CPU build by default.** CUDA libraries are only in the experimental `--gpu` build. No driver is
  ever bundled.
- **Locked dependencies** for the build (`constraints.txt`, resolved for win_amd64 / Python 3.12).

**Verified on `windows-latest`** (Windows Server 2025, no GPU; commit 8150579, v0.3.1 build):
every packaged-app check in the README table passed.
- Sizes: program folder 677 MB, 516 files; ZIP 248 MB; installer 179 MB.

**Still to verify on the user's PC:**
- the installer UI and SmartScreen;
- display on a real screen;
- GPU transcription and NVENC on the RTX 4070;
- performance.

**Still open for Phase 9:**
- code signing (removes the SmartScreen warning);
- an update notification;
- a tested GPU build.

## Remaining phases

Next phase (not started): **Phase 4, silence removal and manual cutting.**

4. **Smart cut**: `silencedetect` parsing, cut-proposal model, accept/reject, manual ranges,
   undo/redo stack, keep-segment merge, caption remapping, and frame-accurate export with
   `trim/atrim` + `concat` in one filter graph.
5. **Resize**: pure geometry helpers for fit/fill/crop/blur (tested), preview overlay, encoder
   settings (H.264/H.265, CRF/CQ, fps, audio).
6. **Batch**: per-video and shared settings, presets, export jobs, filename templates,
   duplicate protection, overall progress, separate queue and render concurrency (render default 1–2).
7. **AI providers**: provider manager with priority/fallback, error classification (auth and
   bad-request errors never fall back), retries with backoff, rate limits, usage display.
8. **Hardware**: `ffmpeg -encoders` plus a 1-frame NVENC test encode, CUDA checks, CPU fallback,
   resource monitor, temp cleanup, benchmarks.
9. **Release**: packaging is done (see above). Remaining: code signing, update notification,
   tested GPU build, release notes per version.

## Risks

- **Qt Multimedia preview** depends on the FFmpeg backend bundled with PySide6. Some codecs
  (ProRes RAW, unusual HEVC profiles) may not preview even when FFmpeg can export them. The preview
  shows an error message in that case; export is unaffected.
- **NVENC session limits**: GeForce drivers limit concurrent NVENC sessions (currently 8 on recent
  drivers). Render concurrency must stay below that.
- **Frame-accurate cutting** needs re-encoding. Stream copy cuts only at keyframes, so it will
  only be offered when exact accuracy isn't needed.
- **Whisper on GPU** needs matching CUDA/cuDNN DLLs. The CPU fallback must always work.
- **Time-varying crop**: solved with `sendcmd` driving `crop` (tested with pixel checks). If a
  future FFmpeg changes `crop` commands, feeding cropped frames into an encoder pipe is the fallback.

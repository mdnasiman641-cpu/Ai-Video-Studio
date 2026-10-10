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
- Phase 3: `opencv-python` and `mediapipe`. **MediaPipe Python runs on the CPU on Windows; it
  does not use CUDA.** On a Ryzen 7 7700, 1080p face detection on downscaled frames should be fast
  enough, but this must be benchmarked. A CUDA alternative (e.g. an ONNX Runtime GPU face detector)
  will only be considered if the measurements call for it.
- Phase 7: official `google-genai` and `openai` SDKs. Keys go in the Windows Credential Manager
  (via `keyring`) or the user DB, never in the repo.
- Phase 8: `psutil` for CPU/RAM. GPU usage via `nvidia-smi` or NVML when present.
- Phase 9: PyInstaller (one-folder build). FFmpeg is only bundled as an LGPL build with its
  license text included.

## Phase plan

1. **Foundation**: done (see README).
2. **Captions**: done (v0.2.0). Windows-readiness verification done in v0.2.1 (see below).

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

Next phase (not started): **Phase 3, face tracking and auto-reframe.**
3. **Tracking/reframe**: detection on sampled frames, IoU/centroid tracker with target selection,
   confidence, smoothing (EMA + dead zone + max speed), loss recovery (hold, then ease to centre),
   crop-path editor with manual keyframes, export with a crop path that varies over time
   (`sendcmd`/per-segment crops or piping frames).
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
9. **Release**: PyInstaller spec, build script and log, launch check of the built exe,
   troubleshooting docs.

## Risks

- **Qt Multimedia preview** depends on the FFmpeg backend bundled with PySide6. Some codecs
  (ProRes RAW, unusual HEVC profiles) may not preview even when FFmpeg can export them. The preview
  shows an error message in that case; export is unaffected.
- **NVENC session limits**: GeForce drivers limit concurrent NVENC sessions (currently 8 on recent
  drivers). Render concurrency must stay below that.
- **Frame-accurate cutting** needs re-encoding. Stream copy cuts only at keyframes, so it will
  only be offered when exact accuracy isn't needed.
- **Whisper on GPU** needs matching CUDA/cuDNN DLLs. The CPU fallback must always work.
- **Time-varying crop** in FFmpeg filters is awkward. Feeding OpenCV-cropped frames into an
  FFmpeg encoder pipe is the fallback.

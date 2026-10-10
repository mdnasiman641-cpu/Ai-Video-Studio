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

- Phase 2: `faster-whisper` (CTranslate2; CUDA 12 + cuDNN 9 needed for GPU, CPU int8 fallback).
  Model weights download at first use to the user cache, never into the repo.
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
2. **Captions**: audio extraction (FFmpeg → 16 kHz mono WAV in the temp dir), faster-whisper job
   with word timestamps and a language option, caption model (segments with words), editor table
   (text/start/end, add/delete/split/merge), styles and presets, SRT import/export, live style
   preview over the video, ASS generation for burn-in. Optional AI correction is shown as a
   reviewable diff and never applied silently.
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

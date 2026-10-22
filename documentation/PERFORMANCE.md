# Export performance: measurements and settings

This page records what was measured, what was changed because of it, and what was not
measured. All numbers come from the app's own code (the same filter graphs, detectors and
encoder arguments the exports use) on short generated clips, never from a full-length render.

## Test machine and clip

- Linux container, Intel Xeon @ 2.8 GHz, **4 cores**, 16 GB RAM, **no GPU**, FFmpeg 6.1.
- 60 s, 1920×1080, 30 fps H.264 clip (moving test pattern with film-grain noise, about
  5.4 Mbit/s), AAC audio with a pause every 3 s. Decoding it alone takes 5.7 s.

A Ryzen 7 7700 (8 cores / 16 threads) is faster than this machine, and an RTX 4070 adds
NVENC, so absolute times on your PC will be lower. The **ratios** are what the changes
were chosen by.

## Where the time went (before)

| Stage, 60 s of 1080p | Time | Notes |
|---|---|---|
| Re-encode only, x264 medium | 50.3 s | decode + encode |
| 1080×1920 **blur background**, x264 medium | **84.2 s** | blurring filter: 60.4 s alone |
| 1080×1920 fit (colour bars), x264 medium | 37.3 s | |
| 1080×1920 fill (crop), x264 medium | 46.1 s | |
| Blur + 20 Smart Cut parts | 70.4 s | output 49 s long |
| Blur + static captions | 91.8 s | |
| Blur + animated captions (Pop In, Fade Out, Karaoke) | 91.3 s | animation costs nothing extra |
| Blur, x264 veryfast | 76.9 s | the encoder was not the bottleneck |
| **Auto Reframe analysis, Auto Face** (faces + HOG persons) | **44.2 s** | per 60 s of video |
| Auto Reframe analysis, faces only | 14.3 s | |
| Per frame (960×540): YuNet face / HOG person | 23 ms / 97 ms | |

Root causes found:

1. **Person detection that no framing used.** In Auto Face and Active Speaker modes the
   framing follows faces only, yet every analysed frame also ran OpenCV's HOG person
   detector, about 4× the cost of face detection. For a 43-minute video at 5 frames/s
   that is roughly 13,000 frames, about 30 minutes on the test machine, which matches the
   30–40 minutes per video seen in bulk exports with Auto Reframe.
2. **Blurred background computed at full output size.** Two passes of a large box blur on
   every 1080×1920 frame took more time than encoding.
3. **Analysis repeated for every export.** Whisper captions, Smart Cut audio levels and
   face tracking ran again for every export of the same file (Retry, another preset or
   another quality).
4. **Speech recognition on the CPU used 4 threads.** The standard Windows build has no CUDA
   libraries, so Whisper runs on the CPU, and faster-whisper uses 4 threads unless told
   otherwise.

## What changed

| Change | Measured result |
|---|---|
| Bulk Auto Face / Active Speaker analyses faces only (no HOG) | analysis 44.2 s → **14.3 s** per 60 s (3.1×) |
| Blur background computed at ¼ size, then scaled up (SSIM 0.98 vs. before) | blur filter 60.4 s → 22.8 s; full export **84.2 s → 54.5 s** (1.55×); with 20 cuts 70.4 → 44.4 s |
| Analysis cache (transcript, audio levels, tracking) per file and settings | a repeated export skips the analysis entirely (tested: second export reports "reused") |
| Optional analysis frame size (Settings → Tracking): 640 px instead of 960 px | faces-only analysis 14.3 s → 9.5 s (default stays 960 px: smaller frames find small faces less reliably) |
| Whisper CPU threads = physical cores (Settings → Captions → CPU threads) | **not measured**: Hugging Face is not reachable from the test machine, so no model could be loaded |
| Export modes Draft…Maximum (x264 ultrafast…slower, NVENC p1…p7) | Draft vs. Maximum file size on a noisy clip: more than 2× apart (tested) |
| Hardware decoding option (`-hwaccel auto`) | **not measured** (no GPU on the test machine); off by default |

Not changed, because measurement showed no problem: caption animation (no extra cost),
the Smart Cut graph (20 cuts made the export faster because the output is shorter), audio
(AAC is copied when nothing changes the audio).

## Which stages use the CPU or the GPU

| Stage | Where it runs |
|---|---|
| Decoding for export | CPU; GPU (NVDEC/D3D11VA/QSV) with Hardware decoding = Auto, if FFmpeg can |
| Resize, blur, colour, captions (libass) | CPU (FFmpeg filters) |
| Encoding | NVENC / Quick Sync / AMF when a test encode proves it works, else CPU (x264/x265) |
| Whisper captions | CUDA GPU only with the optional CUDA libraries (`build.py --gpu`), else CPU |
| Face detection (YuNet), person detection (HOG), mouth movement | CPU (OpenCV) |
| Voice activity (Silero VAD) | CPU |
| Smart Cut audio levels | CPU (FFmpeg) |

## Recommended settings (Bulk Processing → Hardware → Use Recommended)

The detector tests every hardware encoder with a tiny test encode; a GPU's name or driver
alone is never trusted. Recommendations:

- **Encoder:** Auto, which tries NVENC, then Quick Sync, then AMF, then the CPU.
- **Exports at a time:** 2 when a hardware encoder works and the PC has at least 6 cores
  and 16 GB of RAM (the CPU then only decodes and filters), otherwise 1. AI analysis
  (Whisper, face tracking) always runs one video at a time.
- **Whisper CPU threads:** one per physical core.

For a Ryzen 7 7700 + RTX 4070 + 32 GB this gives NVENC, two exports at a time and 8
Whisper threads. These values are computed from the detected hardware, so other PCs get
their own.

# Windows acceptance test (v0.7.x)

This checklist is for testing AI Video Studio on a real Windows 10/11 PC. Everything here has
passed automated tests on GitHub's Windows Server runners, but those runners have **no NVIDIA GPU,
no real screen, no AI keys and no personal videos**. The steps below cover what only a real PC can
show. Please fill in the result table at the end and send it back with the files listed in
"What to send back".

Nothing here makes paid AI calls unless you choose to (steps 9.4 and 9.5 send a few tokens to
the provider you configured, and each asks you to confirm first).

## 0. Get the build

### Option A: download the CI build (recommended)

1. Open https://github.com/mdnasiman641-cpu/Ai-Video-Studio/actions/workflows/build-windows.yml
2. Click the newest run with a green check for branch `claude/bold-goldberg-nhol0w`.
3. Under **Artifacts**, download `ai-video-studio-windows` (a ZIP of about 430 MB; you must be
   signed in to GitHub). It contains:
   - `AI-Video-Studio-<version>-setup-win64.exe`: the installer.
   - `AI-Video-Studio-<version>-portable-win64.zip`: the portable version.
   - `SHA256SUMS.txt` and `build-info.json`.
4. Unzip it, then check the files in PowerShell:
   ```powershell
   cd "$env:USERPROFILE\Downloads\ai-video-studio-windows"
   Get-FileHash -Algorithm SHA256 .\AI-Video-Studio-*-setup-win64.exe, .\AI-Video-Studio-*-portable-win64.zip
   Get-Content .\SHA256SUMS.txt
   ```
   The hashes must match `SHA256SUMS.txt`.

### Option B: build it yourself

Needs Python 3.12 (64-bit), Git and optionally Inno Setup 6 (`winget install JRSoftware.InnoSetup`).

```bat
git clone -b claude/bold-goldberg-nhol0w https://github.com/mdnasiman641-cpu/Ai-Video-Studio.git
cd Ai-Video-Studio
scripts\windows\build.bat
powershell -ExecutionPolicy Bypass -File packaging\windows\test_packaged.ps1 -AppDir dist\AI-Video-Studio
```

The outputs are in `dist\`. The last command is the same packaged-app test that CI runs.

## 1. Install and start-up

| # | Step | Expected |
|---|------|----------|
| 1.1 | Run the setup EXE. If SmartScreen says "Windows protected your PC": **More info → Run anyway** (the program is not code-signed). | The installer opens. "Install for me only" needs no admin rights. |
| 1.2 | Keep the default folder (`%LOCALAPPDATA%\Programs\AI Video Studio`), tick "Create a desktop shortcut", finish. | A Start Menu entry "AI Video Studio" and a folder "AI Video Studio Tools" exist. |
| 1.3 | Start **AI Video Studio** from the Start Menu. | The window opens within a few seconds. No black console window. The status bar shows "FFmpeg: OK". |
| 1.4 | **Help → About**. | Version 0.7.x; data folder `C:\Users\<you>\AppData\Local\AIVideoStudio`. |
| 1.4b | **Settings → Speech recognition models.** (v0.7.0 crashed here on PCs with an old model cache.) | Every model shows Downloaded, Not downloaded, Incomplete download or Cannot be read. A "Cannot be read … can be imported from the old cache" model: select it → **Import Selected** → confirm. The Export Queue shows a copy job (no download), then the model shows Downloaded (`…\models\whisper\small`). |
| 1.5 | Open every sidebar page once. | No page says "planned" or "not implemented"; no error dialogs. |
| 1.6 | Start **AI Video Studio Tools → Check AI Video Studio environment**. | Ends with "Environment OK". Keep this output (see section 10). |
| 1.7 | Start **AI Video Studio Tools → AI Video Studio self-test**. | Ends with "Self-test PASSED". Some checks are SKIPPED without test assets; that is expected. |
| 1.8 | Portable: unzip the portable ZIP to e.g. `D:\AVS-portable`, create an empty file `portable.txt` next to `AI-Video-Studio.exe`, start the EXE. | It starts; a `UserData` folder appears next to the EXE (settings, logs, models). |
| 1.9 | Start the portable EXE from a USB drive or another folder, or from a command prompt opened in a different folder. | Same behaviour: nothing depends on the current folder or the source code. |

## 2. Project save and reopen; captions

Use one of your own 1080p videos (about 1–3 minutes, with speech). The app never changes it.

| # | Step | Expected |
|---|------|----------|
| 2.1 | **File → New Project**, import your video (drag and drop works). | Metadata appears (duration, 1920×1080, fps, audio). The preview plays with sound. |
| 2.2 | Captions → **Generate Captions…**, model `small`, language Auto or English. The first time, the app asks before downloading the model (about 480 MB). | A download job with progress, then a transcription job; captions appear. The status says which device was used (see 2.3). |
| 2.3 | Note the device in the status line or Export Queue message. | Standard CPU build: **CPU**. If the warning says "NVIDIA GPU was found, but the CUDA libraries are missing", that is expected for the CPU build (see "GPU notes"). The app must **not** close or crash. |
| 2.4 | Edit a caption's text and timing, split and merge, change the style. Undo/redo. | Changes show in the preview overlay immediately. |
| 2.5 | **Export SRT…**, open the file in Notepad. | Correct text and times, UTF-8 (Bengali or other scripts readable). |
| 2.6 | **Exact Preview** on a frame with a caption. | The FFmpeg-rendered frame matches the live preview. |
| 2.7 | **File → Save Project**, close the app, start it again. | The project reopens automatically (or from Home → Recent) with captions, style and settings intact. |

## 3. Smart Cut and A/V synchronisation

### 3a. Synchronisation test clip (objective check)

Create a test clip with the bundled FFmpeg. The picture turns white exactly while a tone plays,
with 1 s pauses between tones:

```bat
cd /d "%LOCALAPPDATA%\Programs\AI Video Studio\bin"
ffmpeg -f lavfi -i "color=c=black:s=1920x1080:r=30:d=20" -f lavfi -i "aevalsrc='0.5*sin(2*PI*440*t)*lt(mod(t,2),1)':s=48000:d=20" -filter_complex "[0:v]drawbox=t=fill:c=white:enable='lt(mod(t,2),1)'[v]" -map "[v]" -map 1:a -c:v libx264 -pix_fmt yuv420p -c:a aac -t 20 "%USERPROFILE%\Videos\avs-sync-test.mp4"
```

| # | Step | Expected |
|---|------|----------|
| 3.1 | Import `avs-sync-test.mp4`, open **Smart Cut**, click **Analyze Audio**. | Progress, then about 10 silences proposed. The timeline shows red removed parts between green parts. |
| 3.2 | **Apply Cuts**, then **Export Video…**, H.264, Encoder Automatic. | Export finishes. The duration shown in the result is about 20 − 10 × 0.6 = 14 s. |
| 3.3 | Play the exported file in VLC or the Windows player, frame by frame (VLC: `E` key) around each white flash. | The tone starts with the first white frame **at every cut**, with no growing delay toward the end. A one-frame difference (33 ms at 30 fps) is acceptable. |

### 3b. Your own recording

| # | Step | Expected |
|---|------|----------|
| 3.4 | Your video with pauses → Analyze Audio → look at the proposal. Try **Suggest Threshold**. | Pauses are proposed; speech is kept. With background music, a warning explains that nothing is quiet enough. |
| 3.5 | Mark a proposed silence **Keep**, change a boundary in the table, Undo, Redo. | The table, timeline and durations update; Undo/Redo restore them. |
| 3.6 | **Play edited result**; then **Apply Cuts** and **Export Preview**. | Playback skips the removed parts; the 360p preview opens in your video player. |
| 3.7 | Apply Cuts → Export Video with **Burn in captions**. | Lip sync is correct; captions match the speech after the cuts; no captions from removed parts. |
| 3.8 | **Export SRT…** on the Smart Cut page. | The timings fit the edited video, not the original. |
| 3.9 | Start an export and click **Cancel Export** halfway. | It stops; no half-written `.mp4` (and no `.part` file) is left in the output folder. |

## 4. Auto Reframe and Active Speaker

| # | Step | Expected |
|---|------|----------|
| 4.1 | A talking-head video → **Auto Reframe**, mode **Auto Face Tracking**, 9:16 → **Analyse Video**. | Analysis with progress; subjects listed; the crop rectangle follows the face in the preview. |
| 4.2 | Export 1080×1920. | The face stays in frame; motion is smooth; audio is in sync. |
| 4.3 | A video with two people talking in turns → mode **Face + Speech Audio (Active Speaker)** → Analyse. | The status line at the playhead shows who speaks; the crop moves to the speaker after about the confirmation time, not on every short sound. |
| 4.4 | Note where it chooses the wrong person or switches too often. | Real conversations have **not** been validated yet: please record the times (mm:ss) and what happened. |
| 4.5 | Note how long the analysis takes for how many minutes of video. | Real speed on your PC is not known yet; please report it. |

## 5. Resize & Crop

| # | Step | Expected |
|---|------|----------|
| 5.1 | A 16:9 video → **Resize & Crop**, size 1080×1920, mode **Blur background**. | The canvas preview shows the video in the middle over a blurred copy. Info: 1080×1920 (9:16). |
| 5.2 | Switch to **Fit**, **Fill** (move the horizontal slider), **Crop** (change the region), **Custom** (size and position). | The preview changes immediately; the orange rectangle on the source shows the part used. |
| 5.3 | **Exact Frame Preview** in each mode. | The FFmpeg frame matches the canvas preview (the blur looks a little different on screen; positions and sizes must match). |
| 5.4 | Turn on **Burn in captions** and **Show safe areas**. | Captions appear at the right size on the vertical canvas. |
| 5.5 | Export with H.264 and once with **H.265**. | Both play in VLC; the H.265 file is smaller. |

## 6. Bulk Processing

| # | Step | Expected |
|---|------|----------|
| 6.1 | **Bulk Processing** → Add Folder with 3–5 videos (one of them twice). | Each video is listed once; status changes from Analyzing to Ready. |
| 6.2 | Preset **Vertical Reels 1080x1920** → Apply; choose an output folder; set one video to **Customize Selected…** → 1080×1080. | The Settings column shows Shared/Custom; the Size column shows the target sizes. |
| 6.3 | Put a file named like one of the outputs in the output folder first, then **Start Batch**. | That video's output gets a " (2)" name; the existing file is untouched. |
| 6.4 | **Pause Batch** while the first video renders. | The rendering video finishes; the others show "Waiting (paused)". **Resume** continues. |
| 6.5 | **Cancel Selected** on one waiting video, then **Retry Failed**. | It is cancelled, then rendered. |
| 6.6 | While a batch runs, close the app (or end it in Task Manager), then start it again. | The batch list is back; completed outputs exist; the interrupted video shows Failed ("Interrupted…") and **Retry Failed** renders it. No broken output file under the final name. |
| 6.7 | Watch Task Manager (Performance) during the batch. | Only one video renders at a time by default; the app stays responsive. |

## 7. NVIDIA NVENC export

| # | Step | Expected |
|---|------|----------|
| 7.1 | Run the diagnostics in section 10. | `h264_nvenc` and `hevc_nvenc`: "listed; test encode OK". |
| 7.2 | Settings → Export → edited exports Encoder **Automatic**; export any video (Smart Cut or Resize). | The result says the encoder is `h264_nvenc`. Task Manager → GPU → "Video Encode" shows activity. |
| 7.3 | Same with codec **H.265**. | Encoder `hevc_nvenc`. |
| 7.4 | Encoder **CPU (x264 / x265)**. | Encoder `libx264` / `libx265`; noticeably slower. Compare the times. |
| 7.5 | Bulk: set Settings → Max concurrent renders to 2 and run a batch with NVENC. | Both renders succeed (GeForce cards allow several NVENC sessions). Report the speed against 1. |

If NVENC is listed but the test encode fails, update the NVIDIA driver and run section 10 again.
The app never fails an export because of NVENC: it falls back to the CPU and says so in the
result.

## 8. Captions → AI Check and the AI provider manager

Only do this if you want to use a provider. A free or low-cost tier is enough; each test sends
only a few words.

| # | Step | Expected |
|---|------|----------|
| 8.1 | **AI Models** → select "Google Gemini" (or OpenAI) → **Edit…** → paste your API key → Save. | The API key column shows "Set (Windows Credential Manager)". The key is never shown again. |
| 8.2 | Control Panel → **Credential Manager → Windows Credentials**. | An entry whose name contains `AIVideoStudio` exists. |
| 8.3 | Search for your key in the app's files: `findstr /s /m "<first 12 characters of your key>" "%LOCALAPPDATA%\AIVideoStudio\*"` | **No file is found** (the key is not in the database or logs). |
| 8.4 | **Test Connection**. | "✔ Connected: the key works and N model(s) are available". No prompt is sent. |
| 8.5 | **Enable / Disable** to enable it, then **Test Model** (confirm the dialog). | "✔ Model … answered correctly (… tokens …)". The usage table shows the request, tokens marked "provider", cost "unknown" unless you entered prices. |
| 8.6 | Edit the profile, paste a wrong key, Test Connection. | "✘ … rejected the API key (401/403)". Restore the right key afterwards. |
| 8.7 | Captions → **AI Check…**. | The confirmation names the provider(s) that will receive the caption text. Suggestions appear for review; nothing changes until you apply them. |
| 8.8 | Captions → **Suggest Title…**. | A dialog with a title, description and hashtags to copy. |
| 8.9 | Disable all providers. | AI Check and Suggest Title are greyed out with a hint; everything else works. |

## 9. GPU notes (what to expect)

- **Whisper on the GPU:** the standard build is **CPU-only**. It does not include the CUDA 12
  cuBLAS / cuDNN 9 libraries, so with your RTX 4070 the check reports "CUDA devices: 1" and
  "libraries missing", and 'Automatic' transcribes on the CPU. Since 0.7.1 the app checks for
  these libraries **before** trying CUDA. A missing cuDNN can otherwise end the process inside
  CTranslate2 instead of raising an error that the app could catch.
- **Experimental GPU build:** `python packaging\build.py --gpu` (or install from source with
  `scripts\windows\setup.bat --gpu`). It has not been tested on a GPU. If you try it, report
  whether 'Automatic' says CUDA and how long `small` takes for one minute of speech compared with
  the CPU build.
- **NVENC** does not need these libraries; only the NVIDIA driver.
- **Face detection and speech activity** always run on the CPU.

## 10. Diagnostic commands

These only read information or encode a 0.2-second black test clip; they do not change anything.
Paths assume the default per-user install. For the portable version, use its folder instead.

```bat
:: 1. Full environment check: CUDA devices and libraries, the device "Automatic" will use,
::    NVENC test encodes for H.264 and H.265, FFmpeg source, writable data folders.
"%LOCALAPPDATA%\Programs\AI Video Studio\AI-Video-Studio-Console.exe" --check-env --pause

:: 2. Functional self-test with a JSON report.
"%LOCALAPPDATA%\Programs\AI Video Studio\AI-Video-Studio-Console.exe" --self-test --report "%USERPROFILE%\Desktop\avs-self-test.json" --pause

:: 3. NVIDIA driver and GPU as Windows sees them.
nvidia-smi

:: 4. Encoders compiled into the bundled FFmpeg.
cd /d "%LOCALAPPDATA%\Programs\AI Video Studio\bin"
ffmpeg -hide_banner -encoders | findstr /i "nvenc x264 x265"

:: 5. NVENC test encodes (the same tests the app runs; "error" lines mean not usable).
ffmpeg -hide_banner -f lavfi -i color=c=black:s=256x256:d=0.2 -c:v h264_nvenc -f null -
ffmpeg -hide_banner -f lavfi -i color=c=black:s=256x256:d=0.2 -c:v hevc_nvenc -f null -

:: 6. Where the logs are (attach ai_video_studio.log to a report).
explorer "%LOCALAPPDATA%\AIVideoStudio\logs"
```

From source (a `.venv` made by `scripts\windows\setup.bat`):

```bat
.venv\Scripts\python -m ai_video_studio.tools.check_env
.venv\Scripts\python -c "import ctranslate2; print('CUDA devices:', ctranslate2.get_cuda_device_count())"
```

## 11. What to send back

1. The result table below (Pass / Fail / Notes).
2. The output of command 1 (`--check-env`) and the file `avs-self-test.json` from command 2.
3. The output of commands 3 to 5.
4. For any failure: `%LOCALAPPDATA%\AIVideoStudio\logs\ai_video_studio.log` and
   `startup_error.log` if present. API keys are masked in the log, but please check before
   sharing, and **never send a key**.
5. Timings:
   - Auto Reframe analysis (video length / analysis time);
   - a 1-minute 1080p export with `libx264` and with `h264_nvenc`;
   - transcription of one minute with `small`.

## Result table

| Section | Result (Pass / Fail / Not tested) | Notes |
|---------|-----------------------------------|-------|
| 1 Install and start-up | | |
| 2 Project and captions | | |
| 3a A/V sync test clip | | |
| 3b Smart Cut with your video | | |
| 4 Auto Reframe / Active Speaker | | |
| 5 Resize & Crop | | |
| 6 Bulk Processing | | |
| 7 NVENC | | |
| 8 AI providers and key storage | | |
| Timings | | |

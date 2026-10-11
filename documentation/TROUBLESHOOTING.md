# Troubleshooting

## v0.7.0 does not start: "OSError: [WinError 448] ... untrusted mount point"
**Fixed in v0.7.1.** Install v0.7.1 or newer over v0.7.0; your settings, projects and models are kept.

**Cause.**
- Versions 0.2–0.3 let faster-whisper download Whisper models into
  `%LOCALAPPDATA%\AIVideoStudio\models\models--<org>--<model>\` (the Hugging Face cache layout).
- When Windows allows it (Developer Mode or administrator rights), the files in `snapshots\<revision>\`
  are **symbolic links** to files in `blobs\`.
- v0.7.0 checks every model while building the Settings page, at start-up. Windows refused to follow
  such a link (WinError 448, `ERROR_UNTRUSTED_MOUNT_POINT`), and the error was not handled, so the
  program closed before its window appeared.

**What v0.7.1 does.**
- Model folders are inspected without letting filesystem errors escape. Each model shows a state in
  Settings → Speech recognition models: Downloaded, Not downloaded, Incomplete download, or Cannot be
  read (with the reason in the tooltip and below the table). The same reason is written once to the
  log as "Whisper model is not usable". The rest of the app works normally; transcription asks for
  a usable model.
- A model in the old cache that cannot be read through its links shows "can be imported from the old
  cache". **Import Selected** reads each link as text (without following it), accepts only targets
  inside the cache's own `blobs` folder, and copies the files into `models\whisper\<model>` as plain
  files. Each file is checked against its checksum (the blob file name). Nothing is downloaded, and
  the old cache is not changed or deleted. If the copy fails, the model is downloaded instead, after
  you confirm.

**Checking it yourself** (read-only):
```bat
dir /AL "%LOCALAPPDATA%\AIVideoStudio\models\models--Systran--faster-whisper-small\snapshots\*"
fsutil reparsepoint query "%LOCALAPPDATA%\AIVideoStudio\models\models--Systran--faster-whisper-small\snapshots\<revision>\model.bin"
```
`<SYMLINK>` entries in the `dir` output, or a "Symbolic Link" tag from `fsutil`, confirm the link layout.
Once a model is imported or downloaded into `models\whisper\`, you can delete the old
`models--...` folder yourself if you want the disk space back. The app never deletes it on its own.

## The app does not start
1. Start **AI Video Studio Tools → AI Video Studio (diagnostic console)** from the Start Menu, or run
   `AI-Video-Studio-Console.exe` from the program folder. It shows the log in a console window and
   any Python error.
2. Read `%LOCALAPPDATA%\AIVideoStudio\startup_error.log` (written when the program cannot even load
   its modules, also in the windowed EXE and in portable mode) and the main log
   `%LOCALAPPDATA%\AIVideoStudio\logs\ai_video_studio.log`, where errors after start-up are recorded
   (in the portable version with `portable.txt`: `UserData\logs\` next to the EXE).
3. Run **Check AI Video Studio environment** (`AI-Video-Studio-Console.exe --check-env --pause`) and
   **AI Video Studio self-test** (`--self-test --pause`). The self-test imports a generated video,
   plays it, exports captions and a reframed clip, and reports each step.
4. Common causes:
   - **Files missing or blocked by antivirus.** The `_internal` folder must be next to the EXE. If an
     antivirus program quarantined a file, restore it or reinstall. Do not copy only the EXE out of
     the program folder; use the whole folder (or the installer).
   - **ZIP not fully extracted.** Running the EXE from inside the ZIP in Explorer does not work;
     extract all files first.
   - **"Windows protected your PC" (SmartScreen).** The program is not code-signed. Click
     **More info → Run anyway** after checking the download against `SHA256SUMS.txt`.
   - **32-bit Windows or Windows 7/8.** Not supported; Windows 10/11 64-bit is required.
   - **Unwritable data folder.** The app needs to write to `%LOCALAPPDATA%\AIVideoStudio` (or
     `UserData\` in portable mode). For the portable version on a read-only drive, remove
     `portable.txt`.

## Installing, upgrading and uninstalling
- The installer installs per user by default and needs no admin rights. Upgrading: run the new
  setup EXE over the old installation; settings, models and projects are kept.
- After an uninstall, your settings, logs and models stay in `%LOCALAPPDATA%\AIVideoStudio` unless
  you answered **Yes** to the uninstaller's question. Delete that folder by hand to remove them later.
  Projects in `Documents\AI Video Studio Projects` and exported videos are never removed.

## "FFmpeg: NOT FOUND" in the status bar
The installer and portable ZIP include FFmpeg in the `bin` folder next to `AI-Video-Studio.exe`. If
that folder was removed (or blocked by antivirus software), reinstall, or use one of the options below.
Install FFmpeg (`winget install Gyan.FFmpeg`) and restart the app, or open **Settings → FFmpeg
folder**, choose the folder that contains `ffmpeg.exe` and `ffprobe.exe`, then click
**Detect FFmpeg**. You can also put both executables in a `bin\` folder next to the app.

## A video shows "Error" in the media list
Select it and look at **Details → Error** for FFprobe's message. Common causes: the file is
incomplete (still downloading or recording), or it is not a video. Click **Re-read Metadata**
after fixing it.

## A video shows "Missing"
The file was moved or deleted after import. If you move a project, move its videos with it and
keep their positions relative to the `.avsproj` file; they are relinked automatically.

## Preview shows "This video cannot be previewed"
Qt's built-in decoder could not open the codec. Metadata and (in later phases) export still go
through FFmpeg. Please report the codec shown in Details.

## A job says "Interrupted: the application closed while this job was running."
The app exited while the job was running. Select the job in **Export Queue** and click
**Retry Selected**.

## Auto Reframe says the Haar cascade is used
The YuNet face model could not be downloaded (no internet, proxy or firewall). Analysis still
works with OpenCV's built-in Haar detector, which is less accurate. Connect to the internet and
click **Analyse Video** again, or download `face_detection_yunet_2023mar.onnx` from the OpenCV model
zoo yourself and put it in `%LOCALAPPDATA%\AIVideoStudio\models\`. The file is checked against its
SHA-256 hash; a wrong or damaged file is ignored.

## Auto Reframe follows the wrong person or loses someone
Select the right subject and click **Follow From Start**, or move the playhead to where it goes
wrong and click **Follow From Playhead**. If the person is lost often (turned away, small, fast),
raise **Analyse** (frames per second), turn on full-body detection, or increase **Hold when lost**.
If nobody can be tracked, choose **Center crop (no tracking)** and position it with the offsets.

## Auto Reframe says "Reanalysis required"
The selected tracking mode needs data the stored analysis does not have, for example Active
Speaker needs mouth movement and speech. Click **Analyse Video**. The old results are kept and
are used again if you switch back to a mode they support.

## Active Speaker follows the wrong person or nobody
- Check the **speaker evidence** in the status line while scrubbing. Low values on everyone mean
  the mouths are not visible enough (side views, small faces, masks), the voice is off-screen, or
  two people talk at once. In all these cases the mode deliberately does not guess.
- Raise **Mouth analysis rate** for fast speech, lower **Speaker switch after** for quicker cuts,
  or raise it to avoid cuts on short interjections.
- If the audio is out of sync with the video in the source file itself, mouth movement and speech
  cannot be matched.
- For interviews where you know who speaks when, **Manual Subject Tracking** with **Follow From
  Playhead** gives exact control.

## "OpenCV is not installed"
Run `scripts\windows\setup.bat` again (or `pip install -r requirements.txt`). If both
`opencv-python` and `opencv-python-headless` are installed, uninstall both and reinstall only the
headless one; the non-headless package brings its own Qt libraries.

## Where are the logs?
**Help → Open Logs Folder**, or `%LOCALAPPDATA%\AIVideoStudio\logs`. Attach `ai_video_studio.log`
when reporting a problem. API keys are masked in the log.

## Resetting the app
Close the app and delete `%LOCALAPPDATA%\AIVideoStudio\studio.db` (settings, recent list, job
history). Project files are not affected.

## A Whisper model does not download
Models are downloaded from huggingface.co only after you confirm (Generate Captions, or **Settings →
Speech recognition models → Download**). Check the internet connection, firewall or proxy (the
`HTTPS_PROXY` environment variable is used). An interrupted download resumes where it stopped
when you try again. "Not enough free disk space" means the drive holding
`%LOCALAPPDATA%\AIVideoStudio\models` needs more room. "Checksum does not match" means a file
arrived damaged; it was deleted and will be downloaded again.

To work offline, copy a model folder from another PC into `%LOCALAPPDATA%\AIVideoStudio\models\whisper\`,
or download a faster-whisper (CTranslate2) model folder, for example `Systran/faster-whisper-small`,
and choose it under **Settings → Captions → Local model folder**.

## Captions are generated on the CPU although I have an NVIDIA GPU
This is expected with the standard (CPU) build: it does not include the CUDA 12 cuBLAS / cuDNN 9
libraries, so "Automatic" uses the CPU. `AI-Video-Studio-Console.exe --check-env` lists the missing
DLLs. NVENC video encoding does not need them.
The status line after generation shows the device and the reason for any fallback. GPU
transcription needs the CUDA 12 cuBLAS and cuDNN 9 libraries. The standard installer is a CPU
build and does not include them. From source, run `scripts\windows\setup.bat --gpu` (installs them
as pip packages); for the packaged app, build it with `python packaging\build.py --gpu`
(experimental, not yet tested on a GPU). Keep the NVIDIA driver up to date. `check_env.bat` shows how
many CUDA devices CTranslate2 can see.

## Bengali (or another script) looks wrong in the preview or export
The chosen font must contain the script's glyphs. On Windows, choose a font such as
**Nirmala UI** (built in) or **Noto Sans Bengali**. If the chosen font lacks the glyphs, Qt and
libass each pick a fallback font on their own, and the two may differ. Use **Exact Preview** to
see exactly what the export will look like.

## Export is slow or uses the CPU
**Settings → Export → Video encoder** set to *Automatic* uses NVENC only if a test encode
succeeds. If the export result says NVENC is unavailable, update the NVIDIA driver and use an FFmpeg
build with NVENC (e.g. Gyan "full"). Burning in subtitles always re-encodes the video.

## AI Check is greyed out
AI correction is optional. Choose a provider under **Settings → AI caption correction** and
provide a key, either via the `GEMINI_API_KEY` / `OPENAI_API_KEY` environment variables or
**Store Key** (Windows Credential Manager). Model names change over time; if the provider rejects
the model, enter a current model name from the provider's documentation.

## Smart Cut finds no silence, or wants to remove speech
Silence means "quieter than the threshold" (peak level). With background music or room noise
nothing may be quiet enough: click **Suggest Threshold** or raise the threshold (for example to
−30 dB). If quiet speech is proposed for removal, lower it (for example to −45 dB) or mark those
segments **Keep**. Nothing is cut until you click **Apply Cuts**, and every change can be undone.

## An export says the file already exists or is being written
The source video is never overwritten. An existing file is only replaced when you confirm it, and
two exports can never write the same file at once. Choose another name, or wait for the other
export in the Export Queue. A file ending in `.part.mp4` is an unfinished export (for example after
a crash); it can be deleted.

## Batch videos stay "Waiting"
By default only one video renders at a time (Settings → Max concurrent renders). Videos show
"Waiting (paused)" after **Pause Batch**; click **Resume Batch**. If the whole queue is paused, the
Export Queue page shows "Resume Queue".

## AI Check or Suggest Title is greyed out, or an AI request fails
Open **AI Models**: a provider must be enabled, offer the task (capabilities) and have a key (the
"API key" column says Missing otherwise). Use **Test Connection** (checks the key; no prompt) and
**Test Model** (a tiny prompt). Error kinds: "rejected the API key" means a wrong key; "rate limit"
means wait or add a fallback provider; "rejected the request" means the provider refused this
request (it is then not sent to other providers). The usage history shows every attempt.

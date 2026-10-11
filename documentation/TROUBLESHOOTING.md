# Troubleshooting

## "FFmpeg: NOT FOUND" in the status bar
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

## Caption generation fails with "Could not download the Whisper model"
The model is downloaded from huggingface.co on first use. Check the internet connection, firewall
or proxy. To work offline, download a faster-whisper (CTranslate2) model folder, for example
`Systran/faster-whisper-small`, and choose it under **Settings → Captions → Local model folder**.

## Captions are generated on the CPU although I have an NVIDIA GPU
The status line after generation shows the device and the reason for any fallback. GPU
transcription needs the CUDA 12 cuBLAS and cuDNN 9 libraries: run `scripts\windows\setup.bat --gpu`
(installs them as pip packages) and keep the NVIDIA driver up to date. `check_env.bat` shows how
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

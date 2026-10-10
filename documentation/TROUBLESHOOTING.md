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

## Where are the logs?
**Help → Open Logs Folder**, or `%LOCALAPPDATA%\AIVideoStudio\logs`. Attach `ai_video_studio.log`
when reporting a problem. API keys are masked in the log.

## Resetting the app
Close the app and delete `%LOCALAPPDATA%\AIVideoStudio\studio.db` (settings, recent list, job
history). Project files are not affected.

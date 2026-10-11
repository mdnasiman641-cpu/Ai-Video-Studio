# Installs FFmpeg/FFprobe on a Windows runner and puts them on PATH for later steps.
# Chocolatey first (two attempts); if its feed is unavailable (it has returned 504 errors while
# reporting success), the same pinned Gyan build the app bundles is downloaded from GitHub and
# its SHA-256 is checked. Fails the step when FFmpeg is still not usable.
$ErrorActionPreference = "Stop"
$url = "https://github.com/GyanD/codexffmpeg/releases/download/7.1.1/ffmpeg-7.1.1-essentials_build.zip"
$sha256 = "04861d3339c5ebe38b56c19a15cf2c0cc97f5de4fa8910e4d47e5e6404e4a2d4"

function Test-FFmpeg { return [bool](Get-Command ffmpeg -ErrorAction SilentlyContinue) -and [bool](Get-Command ffprobe -ErrorAction SilentlyContinue) }

for ($attempt = 1; $attempt -le 2 -and -not (Test-FFmpeg); $attempt++) {
    Write-Host "Chocolatey attempt $attempt"
    choco install ffmpeg -y --no-progress
    if (-not (Test-FFmpeg)) { Start-Sleep -Seconds 15 }
}

if (-not (Test-FFmpeg)) {
    Write-Host "Chocolatey did not install FFmpeg; downloading the pinned Gyan build from GitHub"
    $zip = Join-Path $env:RUNNER_TEMP "ffmpeg.zip"
    Invoke-WebRequest -Uri $url -OutFile $zip
    $actual = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
    if ($actual -ne $sha256) { throw "FFmpeg download checksum mismatch: $actual" }
    $dest = Join-Path $env:RUNNER_TEMP "ffmpeg"
    Expand-Archive -Path $zip -DestinationPath $dest -Force
    $bin = (Get-ChildItem -Path $dest -Recurse -Filter ffmpeg.exe | Select-Object -First 1).DirectoryName
    $env:PATH = "$bin;$env:PATH"
    Add-Content -Path $env:GITHUB_PATH -Value $bin
}

if (-not (Test-FFmpeg)) { throw "FFmpeg could not be installed." }
ffmpeg -hide_banner -version | Select-Object -First 1

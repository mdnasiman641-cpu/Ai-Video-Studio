<#
Tests the packaged application, not the source code. Used by .github/workflows/build-windows.yml
and runnable on your own PC (that is how GPU/NVENC detection on your hardware gets checked):

  powershell -ExecutionPolicy Bypass -File packaging\windows\test_packaged.ps1 `
      -AppDir dist\AI-Video-Studio -Assets build\test-assets -WhisperModel tiny `
      -Zip dist\AI-Video-Studio-0.7.1-portable-win64.zip -Installer dist\AI-Video-Studio-0.7.1-setup-win64.exe

Every step runs with a PATH that contains only Windows itself, so Python, pip and any FFmpeg
installed on the machine are invisible to the program.
Steps: GUI start-up (no console), model download, functional self-test (twice, to check that
settings persist across restarts), behaviour without FFmpeg, portable ZIP, installer
(install, start, upgrade, uninstall; user data must survive).
#>
param(
    [Parameter(Mandatory = $true)][string]$AppDir,
    [string]$Assets = "",
    [string]$WhisperModel = "",
    [string]$Zip = "",
    [string]$Installer = "",
    [string]$Work = "$env:TEMP\avs-packaged-test"
)
$ErrorActionPreference = "Stop"
$AppDir = (Resolve-Path $AppDir).Path
$Gui = Join-Path $AppDir "AI-Video-Studio.exe"
$Console = Join-Path $AppDir "AI-Video-Studio-Console.exe"
New-Item -ItemType Directory -Force -Path $Work | Out-Null
$Reports = Join-Path $Work "reports"
New-Item -ItemType Directory -Force -Path $Reports | Out-Null
$Results = New-Object System.Collections.ArrayList

function Step([string]$Name, [scriptblock]$Body) {
    Write-Host "`n=== $Name ===" -ForegroundColor Cyan
    try {
        & $Body
        [void]$Results.Add([pscustomobject]@{ Step = $Name; Result = "PASS" })
        Write-Host "PASS: $Name" -ForegroundColor Green
    } catch {
        [void]$Results.Add([pscustomobject]@{ Step = $Name; Result = "FAIL: $($_.Exception.Message)" })
        Write-Host "FAIL: $Name - $($_.Exception.Message)" -ForegroundColor Red
    }
}

function Run-Exe([string]$Exe, [string[]]$Arguments, [string]$DataDir, [int]$TimeoutSec = 900) {
    # Clean environment: only Windows on PATH; data folder chosen by the test (or the default).
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Exe
    $info.Arguments = ($Arguments | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '
    $info.UseShellExecute = $false
    $info.EnvironmentVariables["PATH"] = "$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\Wbem"
    foreach ($name in @("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "QT_QPA_PLATFORM", "QT_PLUGIN_PATH")) {
        if ($info.EnvironmentVariables.ContainsKey($name)) { $info.EnvironmentVariables.Remove($name) }
    }
    if ($DataDir) { $info.EnvironmentVariables["AVS_DATA_DIR"] = $DataDir }
    elseif ($info.EnvironmentVariables.ContainsKey("AVS_DATA_DIR")) { $info.EnvironmentVariables.Remove("AVS_DATA_DIR") }
    $proc = [System.Diagnostics.Process]::Start($info)
    if (-not $proc.WaitForExit($TimeoutSec * 1000)) { $proc.Kill(); throw "$Exe timed out" }
    return $proc.ExitCode
}

function Check-Report([string]$Path, [string[]]$MustPass) {
    $report = Get-Content $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($check in $report.checks) { Write-Host ("  {0,-8} {1}" -f $check.status, $check.name) }
    if (-not $report.ok) { throw "self-test failed: $($report.failed -join ', ')" }
    foreach ($name in $MustPass) {
        $c = $report.checks | Where-Object { $_.name -eq $name }
        if (-not $c -or $c.status -ne "passed") { throw "check '$name' did not pass ($($c.status): $($c.detail))" }
    }
    return $report
}

if (Get-Command python -ErrorAction SilentlyContinue) {
    Write-Host "Note: Python is installed on this machine; the tests hide it from the program (PATH = Windows only)."
}
$Data = Join-Path $Work "data"

Step "Program folder contents" {
    foreach ($f in @($Gui, $Console, "$AppDir\bin\ffmpeg.exe", "$AppDir\bin\ffprobe.exe",
                     "$AppDir\_internal\models\face_detection_yunet_2023mar.onnx", "$AppDir\README.txt",
                     "$AppDir\licenses\THIRD-PARTY-NOTICES.txt", "$AppDir\licenses\ffmpeg\LICENSE")) {
        if (-not (Test-Path $f)) { throw "missing $f" }
    }
    $python = Get-ChildItem $AppDir -Recurse -Filter "python3*.dll" | Select-Object -First 1
    if (-not $python) { throw "the Python runtime DLL is not bundled" }
    $sub = (Get-Item $Gui).VersionInfo
    Write-Host "  version info: $($sub.ProductName) $($sub.ProductVersion) - $($sub.FileDescription)"
    if ($sub.ProductName -ne "AI Video Studio") { throw "missing version information" }
}

Step "GUI start-up without console (windowed EXE, --smoke-test)" {
    $code = Run-Exe $Gui @("--smoke-test") $Data 180
    if ($code -ne 0) { throw "exit code $code" }
    $log = Join-Path $Data "logs\ai_video_studio.log"
    if (-not (Select-String -Path $log -Pattern "Smoke test passed" -Quiet)) { throw "no 'Smoke test passed' in $log" }
    # The windowed EXE is a GUI-subsystem program: Windows never gives it a console window.
    $bytes = [System.IO.File]::ReadAllBytes($Gui)
    $pe = [BitConverter]::ToInt32($bytes, 0x3C)
    $subsystem = [BitConverter]::ToUInt16($bytes, $pe + 0x5C)
    if ($subsystem -ne 2) { throw "AI-Video-Studio.exe is not a GUI-subsystem executable (subsystem $subsystem)" }
}

Step "Start-up with an old Hugging Face model cache containing links (Settings model status)" {
    # Regression for v0.7.0, which crashed at start-up while inspecting such a cache (WinError 448).
    # Real Windows reparse points: a working symbolic link, a broken one and a broken junction.
    $linkData = Join-Path $Work "data-links"
    $cache = Join-Path $linkData "models\models--Systran--faster-whisper-small"
    $snapshot = Join-Path $cache "snapshots\536b0662742c02347bc0e980a01041f333bce120"
    New-Item -ItemType Directory -Force -Path (Join-Path $cache "blobs"), $snapshot | Out-Null
    $blob = Join-Path $cache "blobs\0123456789abcdef0123456789abcdef01234567"
    Set-Content -Path $blob -Value "{}"
    try {
        New-Item -ItemType SymbolicLink -Path (Join-Path $snapshot "config.json") -Target $blob | Out-Null
        New-Item -ItemType SymbolicLink -Path (Join-Path $snapshot "model.bin") -Target (Join-Path $cache "blobs\missing") | Out-Null
        $gone = Join-Path $Work "junction-target"
        New-Item -ItemType Directory -Force -Path $gone | Out-Null
        New-Item -ItemType Junction -Path (Join-Path $linkData "models\models--Systran--faster-whisper-tiny") -Target $gone | Out-Null
        Remove-Item $gone
    } catch {
        Write-Host "  links not created (needs Developer Mode or administrator rights): $($_.Exception.Message)"
    }
    $code = Run-Exe $Gui @("--smoke-test") $linkData 180
    if ($code -ne 0) { throw "exit code $code" }
    $log = Join-Path $linkData "logs\ai_video_studio.log"
    if (-not (Select-String -Path $log -Pattern "Smoke test passed" -Quiet)) { throw "no 'Smoke test passed' in $log" }
}

if ($WhisperModel) {
    Step "Whisper model download through the packaged downloader ($WhisperModel)" {
        $code = Run-Exe $Console @("--download-model", $WhisperModel) $Data 1800
        if ($code -ne 0) { throw "exit code $code" }
        if (-not (Test-Path (Join-Path $Data "models\whisper\$WhisperModel\model.bin"))) { throw "model not stored" }
    }
}

$selfArgs = @("--self-test", "--report")
$extra = @()
if ($Assets) { $extra += @("--assets", (Resolve-Path $Assets).Path) }
if ($WhisperModel) { $extra += @("--whisper-model", $WhisperModel) }

Step "Functional self-test in the packaged EXE (run 1)" {
    $report = Join-Path $Reports "self-test-1.json"
    $code = Run-Exe $Console ($selfArgs + @($report) + $extra) $Data 1800
    $must = @("FFmpeg and FFprobe discovery", "video metadata (probe job)", "Qt Multimedia playback",
              "captions: SRT and burn-in export", "GPU / CUDA / NVENC detection", "face detection (bundled YuNet)",
              "auto reframe analysis and export", "project save and reopen", "temporary files cleaned up",
              "smart cut: silence detection and cut export", "resize & crop: blur background export and exact frame",
              "bulk processing: presets, safe names, render limit", "AI provider manager (stand-in provider, no network)",
              "start-up with an unreadable model cache (WinError 448, simulated)")
    if ($Assets) { $must += "speech activity (Silero VAD)" }
    if ($WhisperModel) { $must += "Whisper transcription (device auto, CPU fallback)" }
    $r = Check-Report $report $must
    $ff = ($r.checks | Where-Object { $_.name -eq "FFmpeg and FFprobe discovery" }).detail
    if (-not $ff.bundled) { throw "FFmpeg was not taken from the program folder: $($ff.ffmpeg)" }
    $gpu = ($r.checks | Where-Object { $_.name -eq "GPU / CUDA / NVENC detection" }).detail
    Write-Host "  CUDA devices: $($gpu.ctranslate2_cuda_devices); NVENC listed: $($gpu.nvenc_listed); NVENC usable: $($gpu.nvenc_usable)"
    if ($code -ne 0) { throw "exit code $code" }
}

Step "Settings persist across restarts (self-test run 2)" {
    $report = Join-Path $Reports "self-test-2.json"
    $code = Run-Exe $Console ($selfArgs + @($report)) $Data 900
    $r = Check-Report $report @()
    $persist = ($r.checks | Where-Object { $_.name -eq "settings persistence" }).detail
    if (-not $persist.persisted_from_previous_run) { throw "a setting saved by run 1 was not read by run 2" }
    if ($code -ne 0) { throw "exit code $code" }
}

Step "Missing FFmpeg is handled (copy without bin\ffmpeg.exe)" {
    $copy = Join-Path $Work "no-ffmpeg"
    if (Test-Path $copy) { Remove-Item -Recurse -Force $copy }
    Copy-Item -Recurse $AppDir $copy
    Remove-Item -Force (Join-Path $copy "bin\ffmpeg.exe"), (Join-Path $copy "bin\ffprobe.exe")
    $data2 = Join-Path $Work "data-no-ffmpeg"
    $report = Join-Path $Reports "self-test-no-ffmpeg.json"
    $code = Run-Exe (Join-Path $copy "AI-Video-Studio-Console.exe") @("--self-test", "--expect-no-ffmpeg", "--report", $report) $data2 600
    Check-Report $report @("missing FFmpeg is reported") | Out-Null
    if ($code -ne 0) { throw "exit code $code" }
    $code = Run-Exe (Join-Path $copy "AI-Video-Studio.exe") @("--smoke-test") $data2 180
    if ($code -ne 0) { throw "the program did not start without FFmpeg (exit $code)" }
    Remove-Item -Recurse -Force $copy
}

if ($Zip) {
    Step "Portable ZIP (extract, run, portable.txt keeps data next to the EXE)" {
        $dir = Join-Path $Work "portable"
        if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
        Expand-Archive -Path $Zip -DestinationPath $dir
        $exe = Join-Path $dir "AI-Video-Studio\AI-Video-Studio.exe"
        New-Item -ItemType File -Path (Join-Path $dir "AI-Video-Studio\portable.txt") | Out-Null
        $code = Run-Exe $exe @("--smoke-test") "" 180
        if ($code -ne 0) { throw "exit code $code" }
        if (-not (Test-Path (Join-Path $dir "AI-Video-Studio\UserData\logs\ai_video_studio.log"))) {
            throw "portable mode did not keep data in UserData"
        }
        Remove-Item -Recurse -Force $dir
    }
}

if ($Installer) {
    $installDir = Join-Path $Work "installed"
    $userData = Join-Path $env:LOCALAPPDATA "AIVideoStudio"
    $marker = Join-Path $userData "models\keep-me.txt"
    $menu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\AI Video Studio.lnk"
    $uninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{8F2B3C1E-6A57-4C0B-9E1D-3B7F2A9C5D41}_is1"

    Step "Installer: per-user install, shortcuts, uninstall entry" {
        $p = Start-Process -FilePath $Installer -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
            "/CURRENTUSER", "/DIR=`"$installDir`"", "/LOG=`"$Work\install.log`"") -Wait -PassThru
        if ($p.ExitCode -ne 0) { throw "installer exit code $($p.ExitCode)" }
        if (-not (Test-Path "$installDir\AI-Video-Studio.exe")) { throw "program not installed" }
        if (-not (Test-Path $menu)) { throw "Start Menu shortcut missing: $menu" }
        $entry = Get-ItemProperty $uninstallKey
        Write-Host "  uninstall entry: $($entry.DisplayName) $($entry.DisplayVersion)"
        if (-not $entry.DisplayVersion) { throw "no version in the uninstall entry" }
    }

    Step "Installed program starts (default data folder %LOCALAPPDATA%\AIVideoStudio)" {
        $code = Run-Exe "$installDir\AI-Video-Studio.exe" @("--smoke-test") "" 180
        if ($code -ne 0) { throw "exit code $code" }
        if (-not (Test-Path "$userData\logs\ai_video_studio.log")) { throw "no log in $userData" }
        New-Item -ItemType Directory -Force -Path (Split-Path $marker) | Out-Null
        Set-Content -Path $marker -Value "user data that must survive upgrades and uninstall"
    }

    Step "Upgrade over the installed version keeps user data" {
        $p = Start-Process -FilePath $Installer -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
            "/CURRENTUSER", "/LOG=`"$Work\upgrade.log`"") -Wait -PassThru
        if ($p.ExitCode -ne 0) { throw "installer exit code $($p.ExitCode)" }
        if (-not (Test-Path $marker)) { throw "user data was removed by the upgrade" }
        $code = Run-Exe "$installDir\AI-Video-Studio.exe" @("--smoke-test") "" 180
        if ($code -ne 0) { throw "upgraded program did not start (exit $code)" }
    }

    Step "Uninstall removes the program but keeps user data" {
        $p = Start-Process -FilePath "$installDir\unins000.exe" -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART") -Wait -PassThru
        if ($p.ExitCode -ne 0) { throw "uninstaller exit code $($p.ExitCode)" }
        # The uninstaller re-launches itself from a temporary copy and finishes in the background.
        $deadline = (Get-Date).AddSeconds(120)
        while ((Test-Path "$installDir\AI-Video-Studio.exe") -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }
        Start-Sleep -Seconds 2
        if (Test-Path "$installDir\AI-Video-Studio.exe") { throw "program files left behind" }
        if (Test-Path $menu) { throw "Start Menu shortcut left behind" }
        if (Test-Path $uninstallKey) { throw "uninstall entry left behind" }
        if (-not (Test-Path $marker)) { throw "user data was deleted by a silent uninstall" }
    }
}

Write-Host "`n=== Summary ===" -ForegroundColor Cyan
$Results | Format-Table -AutoSize | Out-String -Width 200 | Write-Host
$Results | ConvertTo-Json | Set-Content (Join-Path $Reports "summary.json")
if ($Results | Where-Object { $_.Result -ne "PASS" }) { exit 1 }
exit 0

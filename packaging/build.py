"""Build the packaged application (PyInstaller one-folder), the portable ZIP and the installer.

Usage (from the repository root, with requirements-build.txt installed):

    python packaging/build.py                    # EXE folder + portable ZIP (+ installer if Inno Setup is installed)
    python packaging/build.py --ffmpeg none      # do not bundle FFmpeg (users configure it in Settings)
    python packaging/build.py --ffmpeg-dir C:\\ffmpeg\\bin   # bundle FFmpeg from a folder you provide
    python packaging/build.py --gpu              # experimental: also bundle CUDA 12 cuBLAS/cuDNN (~1 GB)

Outputs (``dist/``):
    AI-Video-Studio/AI-Video-Studio.exe            the application (no console window)
    AI-Video-Studio/AI-Video-Studio-Console.exe    the same with a console (diagnostics, --self-test)
    AI-Video-Studio-<version>-portable-win64.zip
    AI-Video-Studio-<version>-setup-win64.exe      (needs Inno Setup 6: iscc.exe)
    SHA256SUMS.txt, build-info.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
DIST = ROOT / "dist"
APP = "AI-Video-Studio"
WINDOWS = sys.platform == "win32"
EXE = ".exe" if WINDOWS else ""

# FFmpeg 7.1.1 "essentials" build by gyan.dev (GPL v3; libx264, libass, NVENC). Bundled as
# separate programs (the app runs them as processes), with their licence and source pointer.
FFMPEG_URL = "https://github.com/GyanD/codexffmpeg/releases/download/7.1.1/ffmpeg-7.1.1-essentials_build.zip"
FFMPEG_SHA256 = "04861d3339c5ebe38b56c19a15cf2c0cc97f5de4fa8910e4d47e5e6404e4a2d4"

sys.path.insert(0, str(ROOT))


def log(message: str) -> None:
    print(f"[build] {message}", flush=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(url: str, target: Path, expected_sha256: str) -> Path:
    if target.is_file() and sha256(target) == expected_sha256:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    log(f"downloading {url}")
    part = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as response, open(part, "wb") as out:  # noqa: S310
        shutil.copyfileobj(response, out, 1 << 20)
    actual = sha256(part)
    if actual != expected_sha256:
        part.unlink()
        raise SystemExit(f"checksum mismatch for {url}: {actual}")
    part.replace(target)
    return target


def make_assets(assets: Path) -> None:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    subprocess.run([sys.executable, str(ROOT / "packaging" / "make_assets.py"), str(assets)], check=True, env=env)
    from ai_video_studio.reframe.detection import YUNET_SHA256, YUNET_URL

    fetch(YUNET_URL, assets / "models" / "face_detection_yunet_2023mar.onnx", YUNET_SHA256)


def run_pyinstaller(assets: Path, gpu: bool) -> Path:
    env = dict(os.environ, AVS_BUILD_ASSETS=str(assets), AVS_BUILD_GPU="1" if gpu else "0")
    subprocess.run([sys.executable, "-m", "PyInstaller", str(ROOT / "packaging" / "windows" / "ai_video_studio.spec"),
                    "--noconfirm", "--clean", "--distpath", str(DIST), "--workpath", str(BUILD / "pyinstaller")],
                   check=True, env=env, cwd=ROOT)
    return DIST / APP


def bundle_ffmpeg(app_dir: Path, mode: str, ffmpeg_dir: str | None) -> dict:
    bin_dir = app_dir / "bin"
    licenses = app_dir / "licenses" / "ffmpeg"
    if ffmpeg_dir:
        source = Path(ffmpeg_dir)
        bin_dir.mkdir(parents=True, exist_ok=True)
        for name in ("ffmpeg", "ffprobe"):
            shutil.copy2(source / f"{name}{EXE}", bin_dir / f"{name}{EXE}")
        return {"ffmpeg": "copied", "source": str(source)}
    if mode == "none":
        return {"ffmpeg": "not bundled"}
    if not WINDOWS:
        log("FFmpeg is only downloaded for Windows builds; use --ffmpeg-dir elsewhere")
        return {"ffmpeg": "not bundled (not Windows)"}
    archive = fetch(FFMPEG_URL, BUILD / "cache" / Path(FFMPEG_URL).name, FFMPEG_SHA256)
    bin_dir.mkdir(parents=True, exist_ok=True)
    licenses.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            name = member.rsplit("/", 1)[-1]
            if member.endswith(("/bin/ffmpeg.exe", "/bin/ffprobe.exe")):
                (bin_dir / name).write_bytes(zf.read(member))
            elif name in ("LICENSE", "README.txt") and member.count("/") == 1:
                (licenses / name).write_bytes(zf.read(member))
    (licenses / "SOURCE.txt").write_text(
        "FFmpeg 7.1.1 (gyan.dev essentials build), licensed under the GNU GPL v3 (see LICENSE).\n"
        f"Binary: {FFMPEG_URL}\nSource: https://github.com/FFmpeg/FFmpeg/tree/n7.1.1 "
        "(build details and exact commit in README.txt)\n"
        "AI Video Studio runs ffmpeg.exe and ffprobe.exe as separate programs.\n", encoding="utf-8")
    return {"ffmpeg": "bundled", "url": FFMPEG_URL, "sha256": FFMPEG_SHA256}


NOTICES = """AI Video Studio - third-party components
==========================================

This folder contains the following third-party software. Each remains under its own licence.

- Python (PSF License)                         https://www.python.org/
- Qt 6 / PySide6 (LGPL v3)                     https://www.qt.io/ , https://doc.qt.io/qtforpython/
  Qt Multimedia uses FFmpeg libraries shipped with PySide6 (LGPL).
- FFmpeg 7.1.1, gyan.dev essentials build (GPL v3), in bin\\ - see licenses\\ffmpeg
- OpenCV (Apache 2.0)                          https://opencv.org/
- YuNet face detection model (MIT), OpenCV Zoo https://github.com/opencv/opencv_zoo
- faster-whisper (MIT), CTranslate2 (MIT)      https://github.com/SYSTRAN/faster-whisper
- Silero VAD model (MIT, bundled with faster-whisper)
- ONNX Runtime (MIT), NumPy (BSD), PyAV (BSD), tokenizers (Apache 2.0), huggingface_hub (Apache 2.0),
  keyring (MIT)
- Whisper models are NOT included; they are downloaded on request (MIT licence, OpenAI;
  CTranslate2 conversions by SYSTRAN / mobiuslabsgmbh on Hugging Face).

The full licence texts of the Python packages are in the *.dist-info folders inside _internal.
"""

README = """AI Video Studio {version}
===========================

Start:        AI-Video-Studio.exe
Diagnostics:  AI-Video-Studio-Console.exe            (same program, shows a console with live logs)
              AI-Video-Studio-Console.exe --check-env --pause
              AI-Video-Studio-Console.exe --self-test --pause

Your data (settings, logs, downloaded AI models, temporary files) is stored in
%LOCALAPPDATA%\\AIVideoStudio, and projects in Documents\\AI Video Studio Projects - never in
this folder, so updating or deleting this folder keeps them.

Portable use (e.g. on a USB drive): create an empty file named portable.txt next to
AI-Video-Studio.exe. Data is then kept in the UserData folder next to it instead.

FFmpeg: bundled in the bin folder. To use another build, choose its folder in Settings -> FFmpeg.
Speech recognition models are downloaded when you first need them (you are asked first).
GPU: this build transcribes on the CPU unless NVIDIA CUDA 12 cuBLAS/cuDNN 9 libraries are available
on the system; video export uses NVENC only when a test encode succeeds.

If the program does not start, see %LOCALAPPDATA%\\AIVideoStudio\\startup_error.log and
%LOCALAPPDATA%\\AIVideoStudio\\logs\\ai_video_studio.log, or run AI-Video-Studio-Console.exe.
Windows SmartScreen may warn because the program is not code-signed: choose "More info" -> "Run anyway".
"""


def add_docs(app_dir: Path, version: str) -> None:
    (app_dir / "README.txt").write_text(README.format(version=version), encoding="utf-8")
    (app_dir / "licenses").mkdir(exist_ok=True)
    (app_dir / "licenses" / "THIRD-PARTY-NOTICES.txt").write_text(NOTICES, encoding="utf-8")


def verify_layout(app_dir: Path, expect_ffmpeg: bool) -> dict:
    internal = app_dir / "_internal"
    required = [
        app_dir / f"{APP}{EXE}",
        app_dir / f"{APP}-Console{EXE}",
        internal / "models" / "face_detection_yunet_2023mar.onnx",
        internal / "faster_whisper" / "assets" / "silero_vad_v6.onnx",
    ]
    if expect_ffmpeg:
        required += [app_dir / "bin" / f"ffmpeg{EXE}", app_dir / "bin" / f"ffprobe{EXE}"]
    missing = [str(p.relative_to(app_dir)) for p in required if not p.exists()]
    plugins = list(internal.rglob("*ffmpegmediaplugin*"))
    cascades = list(internal.rglob("haarcascade_frontalface_default.xml"))
    if not plugins:
        missing.append("Qt Multimedia FFmpeg plugin")
    if not cascades:
        missing.append("OpenCV Haar cascades")
    if missing:
        raise SystemExit("packaged application is incomplete: " + ", ".join(missing))
    size = sum(f.stat().st_size for f in app_dir.rglob("*") if f.is_file())
    return {"files": sum(1 for f in app_dir.rglob("*") if f.is_file()), "size_mb": round(size / 1e6)}


def make_zip(app_dir: Path, version: str) -> Path:
    target = DIST / f"{APP}-{version}-portable-win64.zip"
    if target.exists():
        target.unlink()
    log(f"creating {target.name}")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(app_dir.rglob("*")):
            if path.is_file():
                zf.write(path, Path(APP) / path.relative_to(app_dir))
    return target


def find_iscc() -> str | None:
    found = shutil.which("iscc")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base:
            candidate = Path(base) / "Inno Setup 6" / "ISCC.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def make_installer(app_dir: Path, version: str, assets: Path) -> Path | None:
    iscc = find_iscc()
    if iscc is None:
        log("Inno Setup (iscc.exe) not found: skipping the installer")
        return None
    subprocess.run([iscc, f"/DAppVersion={version}", f"/DSourceDir={app_dir}", f"/DOutputDir={DIST}",
                    f"/DIconFile={assets / 'app.ico'}", str(ROOT / "packaging" / "windows" / "installer.iss")],
                   check=True)
    target = DIST / f"{APP}-{version}-setup-win64.exe"
    if not target.is_file():
        raise SystemExit(f"installer was not created: {target}")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ffmpeg", choices=("auto", "none"), default="auto",
                        help="auto: bundle the pinned FFmpeg build (Windows); none: do not bundle")
    parser.add_argument("--ffmpeg-dir", help="bundle ffmpeg/ffprobe from this folder instead")
    parser.add_argument("--gpu", action="store_true", help="also bundle CUDA libraries (experimental, ~1 GB)")
    parser.add_argument("--no-zip", action="store_true")
    parser.add_argument("--no-installer", action="store_true")
    args = parser.parse_args()

    from ai_video_studio import __version__ as version

    assets = BUILD / "assets"
    if (DIST / APP).exists():
        shutil.rmtree(DIST / APP)
    log(f"version {version}, Python {platform.python_version()}, {platform.platform()}")
    make_assets(assets)
    app_dir = run_pyinstaller(assets, args.gpu)
    ffmpeg = bundle_ffmpeg(app_dir, args.ffmpeg, args.ffmpeg_dir)
    add_docs(app_dir, version)
    expect_ffmpeg = ffmpeg["ffmpeg"] in ("bundled", "copied")
    layout = verify_layout(app_dir, expect_ffmpeg)
    log(f"application folder: {app_dir} ({layout['files']} files, {layout['size_mb']} MB)")
    outputs = [app_dir / f"{APP}{EXE}"]
    if not args.no_zip:
        outputs.append(make_zip(app_dir, version))
    if not args.no_installer and WINDOWS:
        installer = make_installer(app_dir, version, assets)
        if installer:
            outputs.append(installer)
    info = {"version": version, "python": platform.python_version(), "platform": platform.platform(),
            "gpu_libraries": args.gpu, **ffmpeg, "app_folder": layout,
            "outputs": {p.name: {"size_mb": round(p.stat().st_size / 1e6, 1), "sha256": sha256(p)} for p in outputs}}
    (DIST / "build-info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    (DIST / "SHA256SUMS.txt").write_text("".join(f"{v['sha256']}  {k}\n" for k, v in info["outputs"].items()),
                                         encoding="utf-8")
    for name, data in info["outputs"].items():
        log(f"{name}: {data['size_mb']} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# PyInstaller spec for AI Video Studio (one-folder build).
#
# Build with:  python packaging/build.py   (see that script; it prepares build/assets first)
#
# Two executables share one runtime folder:
#   AI-Video-Studio.exe          windowed (no console) - what users start
#   AI-Video-Studio-Console.exe  same program with a console, for diagnostics and --self-test
# A one-folder build starts quickly and lets FFmpeg and models sit next to it; a one-file
# build would unpack ~600 MB to a temp folder on every start.
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata

ROOT = Path(SPECPATH).resolve().parents[1]
ASSETS = Path(os.environ.get("AVS_BUILD_ASSETS", ROOT / "build" / "assets"))
APP = "AI-Video-Studio"
WINDOWS = sys.platform == "win32"
GPU = os.environ.get("AVS_BUILD_GPU") == "1"

datas = []
datas += collect_data_files("faster_whisper")          # assets/silero_vad_v6.onnx (speech activity)
datas += collect_data_files("cv2", includes=["data/*.xml"])  # Haar cascades (face-detection fallback)
for dist in ("keyring", "faster-whisper", "huggingface-hub", "tqdm", "tokenizers", "ctranslate2",
             "onnxruntime", "av", "numpy", "PySide6", "opencv-python-headless"):
    datas += copy_metadata(dist)                          # importlib.metadata lookups at run time
yunet = ASSETS / "models" / "face_detection_yunet_2023mar.onnx"
if yunet.is_file():
    datas.append((str(yunet), "models"))                 # face detector, so analysis works offline

binaries = []
binaries += collect_dynamic_libs("ctranslate2")
binaries += collect_dynamic_libs("onnxruntime")
if GPU:
    # Optional CUDA 12 cuBLAS/cuDNN 9 from requirements-gpu.txt (about 1 GB). Not built by default.
    binaries += collect_dynamic_libs("nvidia")

hiddenimports = [
    "keyring.backends.Windows", "keyring.backends.fail", "keyring.backends.null",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
]
if GPU:
    hiddenimports += ["nvidia", "nvidia.cublas", "nvidia.cudnn"]

excludes = [
    "tkinter", "matplotlib", "IPython", "jupyter", "pytest", "pytestqt", "PyInstaller", "setuptools", "PIL", "gi",
    "scipy", "pandas", "torch", "tensorflow",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtQuick3D",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtPdf", "PySide6.QtDesigner",
    "PySide6.QtBluetooth", "PySide6.QtSerialPort", "PySide6.QtSql", "PySide6.QtTest",
]

a = Analysis(
    [str(ROOT / "packaging" / "windows" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

icon = str(ASSETS / "app.ico") if (ASSETS / "app.ico").is_file() else None


def executable(name, console, version_file):
    return EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=name,
        console=console,
        icon=icon if WINDOWS else None,
        version=str(ASSETS / version_file) if WINDOWS and (ASSETS / version_file).is_file() else None,
        upx=False,
        disable_windowed_traceback=False,
    )


gui = executable(APP, False, "version_gui.txt")
console = executable(APP + "-Console", True, "version_console.txt")
coll = COLLECT(gui, console, a.binaries, a.datas, upx=False, name=APP)

"""Packaging helpers and how the app behaves when it runs as a packaged executable.

The real packaged executable is built and tested by .github/workflows/build-windows.yml.
"""

import struct
import sys
from pathlib import Path

import pytest

from ai_video_studio.core import ffmpeg_tools, paths

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging"))

import make_assets  # noqa: E402


@pytest.fixture
def frozen(monkeypatch, tmp_path):
    """Pretend to run as <tmp>/app/AI-Video-Studio.exe with resources in <tmp>/app/_internal."""
    app = tmp_path / "app"
    internal = app / "_internal"
    internal.mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "AI-Video-Studio.exe"))
    return app


def test_frozen_paths(frozen, monkeypatch):
    assert paths.is_frozen() and paths.exe_dir() == frozen.resolve()
    assert paths.app_root() == frozen / "_internal"
    monkeypatch.delenv("AVS_DATA_DIR")
    monkeypatch.setenv("LOCALAPPDATA", str(frozen.parent / "localappdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(frozen.parent / "localappdata"))
    data = paths.data_dir()
    assert frozen.resolve() not in data.resolve().parents  # user data never inside the program folder


def test_portable_marker_keeps_data_next_to_the_exe(frozen, monkeypatch):
    monkeypatch.delenv("AVS_DATA_DIR")
    (frozen / paths.PORTABLE_MARKER).write_text("")
    assert paths.data_dir() == frozen.resolve() / "UserData"
    assert paths.models_dir() == frozen.resolve() / "UserData" / "models"


def test_portable_marker_is_ignored_when_not_packaged(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert paths.portable_data_dir() is None


def test_bundled_ffmpeg_next_to_the_exe_is_found(frozen, monkeypatch):
    monkeypatch.setenv("PATH", "")
    exe = ".exe" if sys.platform == "win32" else ""
    assert ffmpeg_tools.find_tool("ffmpeg") is None
    (frozen / "bin").mkdir()
    (frozen / "bin" / f"ffmpeg{exe}").write_bytes(b"")
    assert ffmpeg_tools.find_tool("ffmpeg") == frozen.resolve() / "bin" / f"ffmpeg{exe}"
    configured = frozen.parent / "custom"
    configured.mkdir()
    (configured / f"ffmpeg{exe}").write_bytes(b"")
    assert ffmpeg_tools.find_tool("ffmpeg", str(configured)) == configured / f"ffmpeg{exe}"  # Settings wins


def test_missing_ffmpeg_message_points_to_settings(frozen, monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(Exception, match="Settings"):
        ffmpeg_tools.require_tool("ffprobe")


def test_bundled_face_model_is_used_before_downloading(frozen):
    from ai_video_studio.reframe import detection

    data = b"yunet"
    import hashlib

    models = frozen / "_internal" / "models"
    models.mkdir()
    (models / detection.YUNET_FILE).write_bytes(data)

    def no_network(*a, **k):
        raise AssertionError("must not download")

    found = detection.ensure_face_model(frozen.parent / "user-models", opener=no_network,
                                        sha256=hashlib.sha256(data).hexdigest())
    assert found == models / detection.YUNET_FILE


def test_ico_file_layout():
    data = make_assets.ico_bytes([(16, b"PNG16"), (256, b"PNG256")])
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind, count) == (0, 1, 2)
    w1, h1, _c, _r, _p, bpp, size1, off1 = struct.unpack("<BBBBHHII", data[6:22])
    w2, _h2, _c, _r, _p, _b, size2, off2 = struct.unpack("<BBBBHHII", data[22:38])
    assert (w1, h1, bpp, size1) == (16, 16, 32, 5) and w2 == 0  # 0 = 256 px
    assert data[off1:off1 + size1] == b"PNG16" and data[off2:off2 + size2] == b"PNG256"


def test_version_resource():
    assert make_assets.version_tuple("0.3.1") == (0, 3, 1, 0)
    assert make_assets.version_tuple("1.2.3rc4") == (1, 2, 34, 0)
    text = make_assets.version_resource("0.3.1", "AI Video Studio", "AI-Video-Studio", "AI-Video-Studio.exe")
    assert "filevers=(0, 3, 1, 0)" in text and "'OriginalFilename', 'AI-Video-Studio.exe'" in text


def test_build_files_are_consistent():
    from ai_video_studio import __version__

    spec = (ROOT / "packaging" / "windows" / "ai_video_studio.spec").read_text()
    iss = (ROOT / "packaging" / "windows" / "installer.iss").read_text()
    build = (ROOT / "packaging" / "build.py").read_text()
    assert 'APP = "AI-Video-Studio"' in spec and 'APP = "AI-Video-Studio"' in build
    assert '#define AppExe "AI-Video-Studio.exe"' in iss
    assert "PrivilegesRequired=lowest" in iss            # no administrator rights needed
    assert "UninstallSilent" in iss and "MB_DEFBUTTON2" in iss  # user data kept unless explicitly removed
    constraints = (ROOT / "packaging" / "windows" / "constraints.txt").read_text()
    for name in ("pyside6==", "faster-whisper==", "ctranslate2==", "opencv-python-headless==", "onnxruntime=="):
        assert name in constraints
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert f'version = "{__version__}"' in pyproject

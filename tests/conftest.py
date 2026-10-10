import os
import shutil
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
requires_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="FFmpeg/FFprobe not installed")


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    data = tmp_path / "appdata"
    monkeypatch.setenv("AVS_DATA_DIR", str(data))
    return data


@pytest.fixture
def db(tmp_path):
    from ai_video_studio.storage.database import Database

    database = Database(tmp_path / "test.db")
    yield database
    database.close()


def make_sample_video(path: Path, seconds: float = 1.0, size: str = "320x180", audio: bool = True) -> Path:
    """Generate a tiny test video with FFmpeg (never committed to the repo)."""
    args = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac", "-shortest"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(args, check=True)
    return path


@pytest.fixture(scope="session")
def sample_videos(tmp_path_factory):
    if not HAS_FFMPEG:
        pytest.skip("FFmpeg/FFprobe not installed")
    folder = tmp_path_factory.mktemp("samples")
    return {
        "landscape": make_sample_video(folder / "landscape.mp4"),
        "vertical": make_sample_video(folder / "vertical.mp4", size="180x320", audio=False),
    }

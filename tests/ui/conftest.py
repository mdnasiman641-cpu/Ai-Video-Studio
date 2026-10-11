"""Shared fixtures for GUI tests."""

import subprocess

import pytest


@pytest.fixture(scope="session")
def square_clip(tmp_path_factory):
    path = tmp_path_factory.mktemp("rf") / "square.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x303030:s=640x360:r=25:d=3",
                    "-f", "lavfi", "-i", "color=c=red:s=50x50:r=25:d=3", "-f", "lavfi", "-i", "sine=duration=3",
                    "-filter_complex", "[0:v][1:v]overlay=x='60+150*t':y=140[v]", "-map", "[v]", "-map", "2:a",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "3", str(path)], check=True)
    return path


@pytest.fixture(autouse=True)
def process_deferred_deletes(qapp):
    """Destroy windows closed with ``deleteLater()`` at the end of each test.

    Otherwise they stay half-alive until interpreter exit, where PySide destroys all
    remaining Qt objects in arbitrary order. With Qt Multimedia players among them this
    crashed the test process after all tests had passed (``QObject::~QObject`` inside
    ``PySide::destroyQCoreApplication``, seen in a core dump).
    """
    yield
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    QCoreApplication.processEvents()

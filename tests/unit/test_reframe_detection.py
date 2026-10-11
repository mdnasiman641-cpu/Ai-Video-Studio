"""Detector wrappers. The real OpenCV detectors are exercised on synthetic arrays here;
detection on a real face image is in tests/integration/test_reframe_detectors_real.py."""

import builtins
import hashlib
import io

import pytest

from ai_video_studio.core.errors import DependencyMissingError
from ai_video_studio.reframe import detection
from ai_video_studio.reframe.detection import (
    YUNET_FILE,
    DetectorSet,
    build_detectors,
    ensure_face_model,
    opencv_cuda_devices,
)
from ai_video_studio.reframe.model import Detection

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")


class FakeOpener:
    def __init__(self, data: bytes | None = None, error: Exception | None = None):
        self.data, self.error, self.calls = data, error, []

    def __call__(self, url, timeout):
        self.calls.append(url)
        if self.error:
            raise self.error
        return io.BytesIO(self.data)


def test_model_download_is_verified(tmp_path):
    data = b"model bytes"
    digest = hashlib.sha256(data).hexdigest()
    opener = FakeOpener(data)
    path = ensure_face_model(tmp_path, opener=opener, sha256=digest)
    assert path == tmp_path / YUNET_FILE and path.read_bytes() == data
    assert ensure_face_model(tmp_path, opener=FakeOpener(error=AssertionError("no second download")),
                             sha256=digest) == path


def test_model_with_wrong_checksum_is_rejected_and_not_kept(tmp_path):
    assert ensure_face_model(tmp_path, opener=FakeOpener(b"tampered"), sha256="0" * 64) is None
    assert not any(tmp_path.iterdir())


def test_model_download_failure_and_offline_mode(tmp_path):
    assert ensure_face_model(tmp_path, opener=FakeOpener(error=OSError("offline"))) is None
    assert ensure_face_model(tmp_path, download=False) is None
    assert not any(tmp_path.iterdir())


def test_missing_model_falls_back_to_haar_with_a_warning(tmp_path):
    detectors = build_detectors(tmp_path, detect_persons=True, opener=FakeOpener(error=OSError("offline")))
    assert "Haar" in detectors.description and "HOG" in detectors.description
    assert "CPU" in detectors.description
    assert any("Haar" in w for w in detectors.warnings)
    blank = np.zeros((360, 640, 3), np.uint8)
    assert detectors(blank) == []  # nothing to find in a black frame


def test_detectors_handle_any_frame_size(tmp_path):
    detectors = build_detectors(tmp_path, detect_persons=True, allow_download=False)
    for shape in ((50, 40, 3), (720, 404, 3), (360, 640, 3)):
        assert detectors(np.full(shape, 128, np.uint8)) == []


def test_person_fallback_only_runs_without_faces():
    face = Detection(0.4, 0.2, 0.1, 0.15, 0.9, "face")
    body = Detection(0.3, 0.1, 0.3, 0.8, 0.7, "person")
    calls = []

    def persons(frame):
        calls.append(frame)
        return [body]

    with_face = DetectorSet(lambda f: [face], persons)
    assert with_face("frame") == [face] and calls == []
    without_face = DetectorSet(lambda f: [], persons)
    assert without_face("frame") == [body] and len(calls) == 1
    no_persons = DetectorSet(lambda f: [], None)
    assert no_persons("frame") == []


def test_normalised_boxes_are_clipped_to_the_frame():
    d = detection._normalised(-10, 50, 100, 400, 200, 300, 0.8, "face")
    assert d.x == 0 and d.w == pytest.approx(90 / 200)
    assert d.y + d.h == pytest.approx(1.0)


def test_missing_opencv_gives_a_clear_error(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "cv2":
            raise ImportError("No module named 'cv2'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(DependencyMissingError, match="OpenCV is not installed"):
        detection.load_cv2()
    assert detection.opencv_version() is None
    assert detection.opencv_cuda_devices() == 0


def test_pip_opencv_reports_no_cuda():
    # pip wheels are built without CUDA; detection is documented as CPU-only.
    assert opencv_cuda_devices() == 0

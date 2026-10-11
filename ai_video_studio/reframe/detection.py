"""Face and person detectors built on OpenCV (``opencv-python-headless``).

- Faces: **YuNet** (OpenCV's DNN face detector, ``cv2.FaceDetectorYN``). Its 230 KB ONNX
  model is downloaded once from the OpenCV model zoo into the app's data folder and
  checked against a SHA-256 hash. If it cannot be downloaded, the **Haar cascade**
  bundled with OpenCV is used instead (less accurate, frontal faces only) and a warning
  is shown.
- Persons: OpenCV's **HOG** people detector (bundled), used on frames where no face is
  found. It finds upright, mostly full-body people that are reasonably large in the frame.

The pip OpenCV builds have no CUDA support, so detection always runs on the CPU. Frames
are analysed at a reduced size and rate, which keeps this fast on a desktop CPU.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from ai_video_studio.core.errors import DependencyMissingError
from ai_video_studio.reframe.model import Detection

log = logging.getLogger(__name__)

YUNET_FILE = "face_detection_yunet_2023mar.onnx"
YUNET_URL = ("https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
             "face_detection_yunet/face_detection_yunet_2023mar.onnx")
YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"

FACE_SCORE_THRESHOLD = 0.6
HAAR_FILE = "haarcascade_frontalface_default.xml"


def load_cv2():
    try:
        import cv2
    except ImportError as exc:
        raise DependencyMissingError(
            "OpenCV is not installed, so faces and people cannot be detected. "
            "Run: pip install -r requirements.txt"
        ) from exc
    return cv2


def opencv_version() -> str | None:
    try:
        return load_cv2().__version__
    except DependencyMissingError:
        return None


def opencv_cuda_devices() -> int:
    """CUDA devices usable by OpenCV. The pip wheels are built without CUDA, so this is 0 there."""
    try:
        cv2 = load_cv2()
        return int(cv2.cuda.getCudaEnabledDeviceCount()) if hasattr(cv2, "cuda") else 0
    except Exception:  # noqa: BLE001 - any failure means "no OpenCV CUDA"
        return 0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


Opener = Callable[[str, float], object]  # (url, timeout) -> file-like response


def ensure_face_model(models_dir: Path, download: bool = True, opener: Opener | None = None,
                      url: str = YUNET_URL, sha256: str = YUNET_SHA256) -> Path | None:
    """Path to the verified YuNet model, downloading it if needed. ``None`` if unavailable.

    The packaged application ships a verified copy (``<bundle>/models``), so it works offline.
    """
    from ai_video_studio.core.paths import app_root

    bundled = app_root() / "models" / YUNET_FILE
    if bundled.is_file() and sha256_file(bundled) == sha256:
        return bundled
    target = Path(models_dir) / YUNET_FILE
    if target.is_file():
        if sha256_file(target) == sha256:
            return target
        log.warning("Face model file has the wrong checksum; downloading it again", extra={"path": str(target)})
    if not download:
        return None
    open_url = opener or (lambda u, timeout: urllib.request.urlopen(u, timeout=timeout))  # noqa: S310 - fixed https URL
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".yunet-", dir=target.parent)
    tmp = Path(tmp_name)
    try:
        with open(fd, "wb") as out, open_url(url, 30) as response:
            shutil.copyfileobj(response, out)
        if sha256_file(tmp) != sha256:
            log.warning("Downloaded face model failed the checksum test", extra={"url": url})
            return None
        tmp.replace(target)
        log.info("Face detection model downloaded", extra={"path": str(target)})
        return target
    except OSError as exc:
        log.warning("Face detection model could not be downloaded", extra={"error": str(exc)})
        return None
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


class FrameDetector(Protocol):
    def __call__(self, frame) -> list[Detection]: ...


class YuNetFaceDetector:
    name = "YuNet face detector"

    def __init__(self, model_path: Path, score_threshold: float = FACE_SCORE_THRESHOLD):
        cv2 = load_cv2()
        self._detector = cv2.FaceDetectorYN.create(str(model_path), "", (320, 320), score_threshold, 0.3, 5000)
        self._size: tuple[int, int] | None = None

    def __call__(self, frame) -> list[Detection]:
        h, w = frame.shape[:2]
        if self._size != (w, h):
            self._detector.setInputSize((w, h))
            self._size = (w, h)
        _ok, faces = self._detector.detect(frame)
        if faces is None:
            return []
        result = []
        for f in faces:
            det = _normalised(f[0], f[1], f[2], f[3], w, h, float(f[-1]), "face")
            # Columns 4-13: right eye, left eye, nose tip, right and left mouth corner (x, y).
            points = tuple(float(f[4 + i]) / (w if i % 2 == 0 else h) for i in range(10))
            result.append(Detection(det.x, det.y, det.w, det.h, det.confidence, "face", landmarks=points))
        return result


class HaarFaceDetector:
    """Fallback face detector bundled with OpenCV. Its confidence is a rough score."""

    name = "Haar cascade face detector (fallback)"

    def __init__(self, min_confidence: float = 0.0):
        cv2 = load_cv2()
        self._cv2 = cv2
        self.min_confidence = min_confidence
        self._cascade = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / HAAR_FILE))
        if self._cascade.empty():
            raise DependencyMissingError("OpenCV's bundled face cascade could not be loaded.")

    def __call__(self, frame) -> list[Detection]:
        h, w = frame.shape[:2]
        gray = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2GRAY)
        min_side = max(20, int(min(w, h) * 0.05))
        boxes, _levels, weights = self._cascade.detectMultiScale3(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(min_side, min_side), outputRejectLevels=True)
        result = []
        for (x, y, bw, bh), weight in zip(boxes, weights):
            confidence = max(0.0, min(1.0, float(weight) / 8.0))
            if confidence >= self.min_confidence:
                result.append(_normalised(x, y, bw, bh, w, h, confidence, "face"))
        return result


class HogPersonDetector:
    """OpenCV's default HOG + linear SVM people detector (upright, mostly full-body people)."""

    name = "HOG person detector"

    def __init__(self, min_confidence: float = 0.25):
        cv2 = load_cv2()
        self.min_confidence = min_confidence
        self._hog = cv2.HOGDescriptor()
        self._hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

    def __call__(self, frame) -> list[Detection]:
        h, w = frame.shape[:2]
        if h < 128 or w < 64:
            return []
        boxes, weights = self._hog.detectMultiScale(frame, winStride=(8, 8), padding=(8, 8), scale=1.05)
        result = []
        for (x, y, bw, bh), weight in zip(boxes, weights):
            confidence = max(0.0, min(1.0, float(weight) / 2.0))
            if confidence >= self.min_confidence:
                result.append(_normalised(x, y, bw, bh, w, h, confidence, "person"))
        return result


def _normalised(x, y, bw, bh, w: int, h: int, confidence: float, kind: str) -> Detection:
    x0, y0 = max(0.0, float(x)), max(0.0, float(y))
    x1, y1 = min(float(w), float(x) + float(bw)), min(float(h), float(y) + float(bh))
    return Detection(x0 / w, y0 / h, max(0.0, x1 - x0) / w, max(0.0, y1 - y0) / h, confidence, kind)


@dataclass
class DetectorSet:
    """The detectors used for one analysis, combined into a per-frame function."""

    face: FrameDetector
    person: FrameDetector | None = None
    description: str = ""
    warnings: list[str] = field(default_factory=list)

    def __call__(self, frame) -> list[Detection]:
        faces = [d for d in self.face(frame) if d.w > 0 and d.h > 0]
        if faces or self.person is None:
            return faces
        return [d for d in self.person(frame) if d.w > 0 and d.h > 0]


def build_detectors(models_dir: Path, detect_persons: bool, allow_download: bool = True,
                    opener: Opener | None = None, min_confidence: float = FACE_SCORE_THRESHOLD) -> DetectorSet:
    """Face detector (YuNet, or Haar as fallback) and optional person detector.

    ``min_confidence`` is the lowest detection score kept. Haar has no landmarks, so mouth
    movement (active-speaker mode) is only available with YuNet.
    """
    load_cv2()
    warnings: list[str] = []
    model = ensure_face_model(models_dir, download=allow_download, opener=opener)
    face: FrameDetector
    if model is not None:
        try:
            face = YuNetFaceDetector(model, score_threshold=min_confidence)
        except Exception as exc:  # noqa: BLE001 - e.g. an OpenCV build without the DNN module
            log.warning("YuNet face detector could not be created", extra={"error": str(exc)})
            face = HaarFaceDetector()  # its score is not comparable with YuNet's
            warnings.append(f"The YuNet face detector could not be started ({exc}); using the less accurate "
                            f"Haar cascade instead.")
    else:
        face = HaarFaceDetector()  # its score is not comparable with YuNet's
        warnings.append("The YuNet face model could not be downloaded, so the less accurate Haar cascade "
                        "(frontal faces only) is used. Connect to the internet and analyse again for better "
                        "results.")
    person = HogPersonDetector(max(0.25, min_confidence - 0.3)) if detect_persons else None
    names = [face.name] + ([person.name] if person else [])
    return DetectorSet(face, person, " + ".join(names) + " (OpenCV, CPU)", warnings)


APPEARANCE_BINS = (8, 4)  # hue x saturation bins per region


def appearance_descriptor(frame, det: Detection) -> tuple[float, ...] | None:
    """Colour histogram of the subject (identity evidence that survives small movements).

    For a face: the face box plus the area below it (neck, clothes), which separates people
    much better than the face alone. For a full body: the upper half of the box. The result is
    normalised so two descriptors can be compared by histogram intersection (0..1).
    """
    cv2 = load_cv2()
    h, w = frame.shape[:2]
    x0, y0, bw, bh = det.x * w, det.y * h, det.w * w, det.h * h
    if det.kind == "person":
        regions = [(x0, y0, bw, bh * 0.5)]
    else:
        regions = [(x0, y0, bw, bh), (x0 - bw * 0.5, y0 + bh * 1.2, bw * 2.0, bh * 1.2)]
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    values: list[float] = []
    for rx, ry, rw, rh in regions:
        x1, y1 = int(max(0, rx)), int(max(0, ry))
        x2, y2 = int(min(w, rx + rw)), int(min(h, ry + rh))
        bins = APPEARANCE_BINS[0] * APPEARANCE_BINS[1]
        if x2 - x1 < 4 or y2 - y1 < 4:
            values += [0.0] * bins
            continue
        hist = cv2.calcHist([hsv[y1:y2, x1:x2]], [0, 1], None, list(APPEARANCE_BINS), [0, 180, 0, 256])
        values += [float(v) for v in hist.flatten()]
    total = sum(values)
    if total <= 0:
        return None
    return tuple(v / total for v in values)

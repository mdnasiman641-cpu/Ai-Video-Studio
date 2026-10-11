"""Real OpenCV face detection and tracking on a real face photo moving through a video.

The image is the public-domain NASA portrait of astronaut Eileen Collins that ships with
scikit-image (``astronaut.png``). It is downloaded at test time, never committed. Set
``AVS_TEST_REFRAME_ASSETS`` to a folder containing ``astronaut.png`` and, optionally,
the YuNet model (``face_detection_yunet_2023mar.onnx``); CI downloads both:

    curl -L -o assets/astronaut.png https://raw.githubusercontent.com/scikit-image/scikit-image/v0.19.3/skimage/data/astronaut.png
    curl -L -o assets/face_detection_yunet_2023mar.onnx <detection.YUNET_URL>
"""

import os
import subprocess
from pathlib import Path

import pytest

from ai_video_studio.reframe.analysis import analyze_video
from ai_video_studio.reframe.detection import YUNET_FILE, build_detectors, sha256_file
from ai_video_studio.reframe.framing import compute_crop_path
from ai_video_studio.reframe.model import ReframeSettings
from tests.conftest import requires_ffmpeg

pytest.importorskip("cv2")
ASSETS = os.environ.get("AVS_TEST_REFRAME_ASSETS")
IMAGE_SHA256 = "88431cd9653ccd539741b555fb0a46b61558b301d4110412b5bc28b5e3ea6cb5"
IMAGE = Path(ASSETS) / "astronaut.png" if ASSETS else None
AVAILABLE = IMAGE is not None and IMAGE.is_file()

if not AVAILABLE and os.environ.get("AVS_REQUIRE_REAL_DETECTOR") == "1":
    raise RuntimeError(f"AVS_REQUIRE_REAL_DETECTOR=1 but no test image at {IMAGE!r}")

pytestmark = [requires_ffmpeg, pytest.mark.skipif(
    not AVAILABLE, reason="AVS_TEST_REFRAME_ASSETS not set (real face detection not tested)")]

W, H, SECONDS = 1280, 720, 6
FACE = 300  # size of the scaled photo; the face itself is about a third of it


@pytest.fixture(scope="module")
def assets():
    assert sha256_file(IMAGE) == IMAGE_SHA256, "unexpected test image"
    return Path(ASSETS)


def build_clip(path, overlays):
    """``overlays``: list of (x expression, y, mirrored) for copies of the photo."""
    args = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=0x283040:s={W}x{H}:r=30:d={SECONDS}"]
    graph, last = [], "0:v"
    for i, (x, y, mirrored) in enumerate(overlays):
        args += ["-loop", "1", "-i", str(IMAGE)]
        flip = ",hflip" if mirrored else ""
        graph.append(f"[{i + 1}:v]scale={FACE}:{FACE}{flip}[p{i}]")
        graph.append(f"[{last}][p{i}]overlay=x='{x}':y={y}:shortest=1[v{i}]")
        last = f"v{i}"
    args += ["-filter_complex", ";".join(graph), "-map", f"[{last}]", "-t", str(SECONDS),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(args, check=True)
    return path


def analyse(clip, detectors, fps=5):
    return analyze_video("ffmpeg", str(clip), W, H, SECONDS, fps, detectors, detectors.description)


def test_yunet_tracks_a_moving_face_and_the_crop_follows(assets, tmp_path):
    if not (assets / YUNET_FILE).is_file():
        pytest.skip("YuNet model not in AVS_TEST_REFRAME_ASSETS")
    clip = build_clip(tmp_path / "one face.mp4", [("50+120*t", 200, False)])
    detectors = build_detectors(assets, detect_persons=False, allow_download=False)
    assert "YuNet" in detectors.description and not detectors.warnings
    analysis = analyse(clip, detectors)
    faces = [t for t in analysis.tracks if t.kind == "face"]
    print(f"\n[real yunet] tracks={[(t.id, len(t.samples), round(t.mean_confidence, 2)) for t in analysis.tracks]}")
    assert len(faces) == 1, "one face should give one track"
    face = faces[0]
    assert analysis.coverage(face) > 0.9 and face.mean_confidence > 0.8
    for s in face.samples:
        photo_centre = 50 + 120 * s.t + FACE / 2
        assert abs(s.cx * W - photo_centre) < FACE * 0.25, s  # the face is inside the moving photo
    xs = [s.cx for s in face.samples]
    assert xs[-1] - xs[0] > 0.45  # moved ~650 px to the right
    path = compute_crop_path(analysis, ReframeSettings(caption_safe="off"), W, H, SECONDS)
    for t in (1.0, 3.0, 5.0):
        sample = face.box_at(t, 0.15, 1.0)
        x, _y = path.at(t)
        assert abs(x + path.crop_w / 2 - sample.cx * W) < 40


def test_yunet_keeps_two_people_apart(assets, tmp_path):
    if not (assets / YUNET_FILE).is_file():
        pytest.skip("YuNet model not in AVS_TEST_REFRAME_ASSETS")
    clip = build_clip(tmp_path / "two faces.mp4", [("80", 220, False), ("900-60*t", 180, True)])
    analysis = analyse(clip, build_detectors(assets, detect_persons=False, allow_download=False))
    faces = sorted((t for t in analysis.tracks if t.kind == "face"), key=lambda t: t.samples[0].cx)
    print(f"\n[real yunet/two] tracks={[(t.id, len(t.samples)) for t in analysis.tracks]}")
    assert len(faces) == 2
    left, right = faces
    assert max(s.cx for s in left.samples) < min(s.cx for s in right.samples), "IDs swapped between people"
    assert analysis.coverage(left) > 0.9 and analysis.coverage(right) > 0.9
    path = compute_crop_path(analysis, ReframeSettings(target=right.id, caption_safe="off"), W, H, SECONDS)
    assert set(path.followed) == {right.id}
    x, _y = path.at(3.0)
    assert abs(x + path.crop_w / 2 - right.box_at(3.0, 0.15, 1.0).cx * W) < 40


def test_haar_fallback_finds_the_face_without_the_model(assets, tmp_path):
    clip = build_clip(tmp_path / "haar.mp4", [("200+60*t", 200, False)])
    detectors = build_detectors(tmp_path / "no-models", detect_persons=False, allow_download=False)
    assert "Haar" in detectors.description and detectors.warnings
    analysis = analyse(clip, detectors)
    best = max(analysis.tracks, key=lambda t: len(t.samples))
    print(f"\n[real haar] tracks={[(t.id, len(t.samples)) for t in analysis.tracks]}")
    assert analysis.coverage(best) > 0.7

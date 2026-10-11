"""Auto reframe jobs against real FFmpeg.

The clip contains a red square moving left to right. A colour-threshold detector stands in
for the face detector, so these tests check the real pipeline around it deterministically:
FFmpeg frame decoding and timing, tracking, crop path, the ``sendcmd`` time-varying crop,
scaling, caption burn-in, audio, frame rate, cancellation and error handling. The real
OpenCV detectors are tested in test_reframe_detectors_real.py.
"""

import subprocess
import threading
import time

import pytest

from ai_video_studio.captions.ass import build_ass
from ai_video_studio.captions.model import CaptionSegment
from ai_video_studio.captions.style import CaptionStyle
from ai_video_studio.core.ffmpeg_tools import find_tool, probe_video
from ai_video_studio.jobs.manager import JobManager
from ai_video_studio.jobs.models import JobState
from ai_video_studio.jobs.reframe_jobs import (
    REFRAME_ANALYZE_JOB,
    REFRAME_EXPORT_JOB,
    REFRAME_PREVIEW_JOB,
    make_reframe_analyze_handler,
    make_reframe_export_handler,
    make_reframe_preview_handler,
)
from ai_video_studio.jobs.store import JobStore
from ai_video_studio.reframe.detection import DetectorSet
from ai_video_studio.reframe.framing import compute_crop_path
from ai_video_studio.reframe.model import Detection, ReframeSettings, TrackingAnalysis
from tests.conftest import requires_ffmpeg

np = pytest.importorskip("numpy")
pytestmark = requires_ffmpeg

W, H, FPS, SECONDS = 640, 360, 30, 4
SQUARE = 60


def square_x(t):
    """Left edge of the red square at time t (pixels)."""
    return 40 + 120 * t


def ffmpeg():
    return find_tool("ffmpeg")


def red_detector(frame):
    """Finds the red square in a BGR frame."""
    mask = (frame[:, :, 2] > 180) & (frame[:, :, 1] < 80) & (frame[:, :, 0] < 80)
    ys, xs = np.nonzero(mask)
    if len(xs) < 20:
        return []
    h, w = mask.shape
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    return [Detection(x0 / w, y0 / h, (x1 - x0) / w, (y1 - y0) / h, 0.95, "face")]


def fake_detectors(_payload):
    return DetectorSet(red_detector, None, "red-square test detector", [])


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    path = tmp_path_factory.mktemp("reframe") / "moving square.mp4"  # space in name on purpose
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=0x303030:s={W}x{H}:r={FPS}:d={SECONDS}",
        "-f", "lavfi", "-i", f"color=c=red:s={SQUARE}x{SQUARE}:r={FPS}:d={SECONDS}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={SECONDS}",
        "-filter_complex", "[0:v][1:v]overlay=x='40+120*t':y=150[v]",
        "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-t", str(SECONDS), str(path)], check=True)
    return path


@pytest.fixture
def manager(db):
    m = JobManager(JobStore(db), 2)
    m.register(REFRAME_ANALYZE_JOB, make_reframe_analyze_handler(ffmpeg, fake_detectors))
    m.register(REFRAME_EXPORT_JOB, make_reframe_export_handler(ffmpeg))
    m.register(REFRAME_PREVIEW_JOB, make_reframe_preview_handler(ffmpeg))
    yield m
    m.shutdown()


def run_job(manager, kind, payload, timeout=120):
    job = manager.submit(kind, kind, payload)
    assert manager.wait_idle(timeout)
    return manager.store.get(job.id)


def analyse(manager, clip, fps=5):
    job = run_job(manager, REFRAME_ANALYZE_JOB, {"path": str(clip), "media_id": "m", "width": W, "height": H,
                                                 "duration": SECONDS, "sample_fps": fps})
    assert job.state is JobState.COMPLETED, job.error
    return TrackingAnalysis.from_dict(job.result["analysis"])


def rgb_frame(path, at, w, h):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(h, w, 3)


def red_centre_x(frame):
    mask = (frame[:, :, 0] > 170) & (frame[:, :, 1] < 90) & (frame[:, :, 2] < 90)
    xs = np.nonzero(mask)[1]
    assert len(xs) > 0, "the subject is not in the output frame"
    return (xs.min() + xs.max()) / 2


def export_payload(clip, analysis, tmp_path, name, settings=None, out=(180, 320), ass=None, has_audio=True):
    path = compute_crop_path(analysis, settings or ReframeSettings(caption_safe="off"), W, H, SECONDS)
    return path, {
        "path": str(clip), "media_id": "m", "output": str(tmp_path / name),
        "src_w": W, "src_h": H, "crop_w": path.crop_w, "crop_h": path.crop_h, "keyframes": path.keyframes(),
        "out_w": out[0], "out_h": out[1], "fps": FPS, "duration": SECONDS, "ass": ass,
        "audio_codec": "aac", "has_audio": has_audio, "encoder": "libx264", "quality": 23,
    }


def test_analysis_tracks_the_subject_with_correct_timing(manager, clip):
    analysis = analyse(manager, clip)
    assert analysis.frames_analyzed == SECONDS * 5
    assert len(analysis.tracks) == 1 and analysis.tracks[0].kind == "face"
    assert analysis.source_size and not analysis.warnings
    for s in analysis.tracks[0].samples:
        # Frame i of the analysis is the source frame at i / sample_fps: positions match the clock.
        assert abs(s.x * W - square_x(s.t)) < 3, s
        assert abs(s.y * H - 150) < 3


def test_export_follows_the_subject(manager, clip, tmp_path):
    analysis = analyse(manager, clip)
    path, payload = export_payload(clip, analysis, tmp_path, "vertical.mp4")
    assert not path.is_static
    job = run_job(manager, REFRAME_EXPORT_JOB, payload)
    assert job.state is JobState.COMPLETED, job.error
    assert job.result["moving_crop"] and job.result["encoder"] == "libx264"
    out = tmp_path / "vertical.mp4"
    meta = probe_video(out, find_tool("ffprobe"))
    assert (meta.width, meta.height) == (180, 320)
    assert meta.has_audio and meta.audio_codec == "aac"
    assert abs(meta.fps - FPS) < 0.01 and abs(meta.duration - SECONDS) < 0.15
    assert not list(tmp_path.glob("*.part*"))
    for t in (0.5, 1.5, 2.5, 3.5):
        x = red_centre_x(rgb_frame(out, t, 180, 320))
        assert abs(x - 90) < 20, f"subject not centred at {t}s (x={x})"
    # A fixed centre crop would show the square far off-centre (or not at all) at these times.
    centre_left = (W - path.crop_w) / 2
    assert square_x(0.5) + SQUARE / 2 < centre_left


def test_export_with_captions_and_without_audio(manager, clip, tmp_path):
    analysis = analyse(manager, clip)
    ass = build_ass([CaptionSegment(0, 4, "Hello reframe")], CaptionStyle(font_size=90), 360, 640)
    _path, payload = export_payload(clip, analysis, tmp_path, "captioned.mp4", out=(360, 640), ass=ass,
                                    has_audio=False)
    job = run_job(manager, REFRAME_EXPORT_JOB, payload)
    assert job.state is JobState.COMPLETED, job.error
    _path, payload = export_payload(clip, analysis, tmp_path, "plain.mp4", out=(360, 640), has_audio=False)
    assert run_job(manager, REFRAME_EXPORT_JOB, payload).state is JobState.COMPLETED
    captioned = rgb_frame(tmp_path / "captioned.mp4", 2.0, 360, 640).astype(int)
    plain = rgb_frame(tmp_path / "plain.mp4", 2.0, 360, 640).astype(int)
    diff = np.abs(captioned - plain).mean(axis=2)
    assert diff[520:620].mean() > 5    # caption drawn near the bottom of the output frame
    assert diff[0:200].mean() < 1      # the rest of the frame is the same crop
    assert not probe_video(tmp_path / "captioned.mp4", find_tool("ffprobe")).has_audio


def test_centre_crop_export_has_no_moving_crop(manager, clip, tmp_path):
    _path, payload = export_payload(clip, None, tmp_path, "centre.mp4", settings=ReframeSettings(mode="center"))
    job = run_job(manager, REFRAME_EXPORT_JOB, payload)
    assert job.state is JobState.COMPLETED, job.error
    assert job.result["moving_crop"] is False


def test_exact_frame_preview_matches_the_export(manager, clip, tmp_path):
    analysis = analyse(manager, clip)
    path, payload = export_payload(clip, analysis, tmp_path, "unused.mp4")
    job = run_job(manager, REFRAME_PREVIEW_JOB, payload | {"at": 2.0})
    assert job.state is JobState.COMPLETED, job.error
    image = job.result["image"]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", image, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         check=True, capture_output=True).stdout
    frame = np.frombuffer(raw, np.uint8).reshape(320, 180, 3)
    assert abs(red_centre_x(frame) - 90) < 20


def test_export_refuses_to_overwrite_source(manager, clip, tmp_path):
    analysis = analyse(manager, clip)
    _path, payload = export_payload(clip, analysis, tmp_path, "x.mp4")
    payload["output"] = str(clip)
    job = run_job(manager, REFRAME_EXPORT_JOB, payload)
    assert job.state is JobState.FAILED and "different from the source" in job.error


def test_corrupt_and_missing_files_fail_with_clear_errors(manager, tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"garbage" * 500)
    job = run_job(manager, REFRAME_ANALYZE_JOB, {"path": str(broken), "width": W, "height": H, "duration": 4})
    assert job.state is JobState.FAILED and "could not be decoded" in job.error
    job = run_job(manager, REFRAME_ANALYZE_JOB, {"path": str(tmp_path / "gone.mp4"), "width": W, "height": H})
    assert job.state is JobState.FAILED and "not found" in job.error


def test_video_without_subjects_reports_it(manager, tmp_path):
    empty = tmp_path / "empty.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=gray:s={W}x{H}:r=25:d=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(empty)], check=True)
    analysis = analyse(manager, empty)
    assert analysis.tracks == [] and any("No faces or people" in w for w in analysis.warnings)


def test_analysis_and_export_can_be_cancelled(db, tmp_path):
    long_clip = tmp_path / "long.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=60",
                    "-c:v", "libx264", "-preset", "ultrafast", str(long_clip)], check=True)

    def slow_detectors(_payload):
        def detect(frame):
            time.sleep(0.05)
            return []
        return DetectorSet(detect, None, "slow", [])

    manager = JobManager(JobStore(db), 2)
    manager.register(REFRAME_ANALYZE_JOB, make_reframe_analyze_handler(ffmpeg, slow_detectors))
    manager.register(REFRAME_EXPORT_JOB, make_reframe_export_handler(ffmpeg))
    started = threading.Event()
    manager.add_listener(lambda j: started.set() if j.progress > 0 else None)
    try:
        job = manager.submit(REFRAME_ANALYZE_JOB, "a", {"path": str(long_clip), "width": 1280, "height": 720,
                                                        "duration": 60, "sample_fps": 10})
        assert started.wait(60)
        manager.cancel(job.id)
        assert manager.wait_idle(30)
        assert manager.store.get(job.id).state is JobState.CANCELLED

        started.clear()
        settings = ReframeSettings(mode="center", preset="9:16")
        path = compute_crop_path(None, settings, 1280, 720, 60)
        out = tmp_path / "cancelled.mp4"
        payload = {"path": str(long_clip), "output": str(out), "src_w": 1280, "src_h": 720, "crop_w": path.crop_w,
                   "crop_h": path.crop_h, "keyframes": [[0, 0, 0], [60, 800, 0]], "out_w": 720, "out_h": 1280,
                   "fps": 30, "duration": 60, "has_audio": False, "encoder": "libx264"}
        job = manager.submit(REFRAME_EXPORT_JOB, "e", payload)
        assert started.wait(60)
        manager.cancel(job.id)
        assert manager.wait_idle(30)
        assert manager.store.get(job.id).state is JobState.CANCELLED
        assert not out.exists() and not list(tmp_path.glob("*.part*"))
    finally:
        manager.shutdown()

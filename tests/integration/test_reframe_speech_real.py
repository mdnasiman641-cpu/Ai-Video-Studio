"""Speech analysis with real FFmpeg and the real Silero VAD (bundled with faster-whisper; no download).

The speech fixture is a synthetic voice (see tests/fixtures/README.md). These tests check
voice activity and timing, not who is speaking.
"""

import subprocess
from pathlib import Path

import pytest

from ai_video_studio.core.errors import MediaError
from ai_video_studio.core.ffmpeg_tools import find_tool
from ai_video_studio.jobs.manager import JobManager
from ai_video_studio.jobs.models import JobState
from ai_video_studio.jobs.reframe_jobs import REFRAME_ANALYZE_JOB, make_reframe_analyze_handler
from ai_video_studio.jobs.store import JobStore
from ai_video_studio.reframe.detection import DetectorSet
from ai_video_studio.reframe.model import ReframeSettings, TrackingAnalysis, reanalysis_reasons
from ai_video_studio.reframe.speech import analyse_speech, extract_speech_audio
from tests.conftest import requires_ffmpeg
from tests.integration.test_reframe_jobs_real import red_detector

pytest.importorskip("faster_whisper")
pytestmark = requires_ffmpeg
SPEECH = Path(__file__).resolve().parents[1] / "fixtures" / "speech_en.flac"


def segments(path, tmp_path):
    wav = tmp_path / "speech.wav"
    extract_speech_audio("ffmpeg", str(path), str(wav))
    return analyse_speech(str(wav)).segments(0.5)


def test_silence_speech_and_a_steady_tone(tmp_path):
    clip = tmp_path / "mix.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x180:r=25:d=10.5",
                    "-f", "lavfi", "-t", "1.5", "-i", "anullsrc=r=16000:cl=mono", "-i", str(SPEECH),
                    "-f", "lavfi", "-t", "3", "-i", "sine=frequency=440",
                    "-filter_complex", "[1:a][2:a][3:a]concat=n=3:v=0:a=1[a]", "-map", "0:v", "-map", "[a]",
                    "-c:v", "libx264", "-c:a", "aac", "-shortest", str(clip)], check=True)
    found = segments(clip, tmp_path)
    print(f"\n[real vad] segments={found}")
    assert found, "no speech found in the speech fixture"
    assert 1.3 < found[0][0] < 2.0         # starts after the 1.5 s of silence
    assert found[-1][1] < 8.2               # nothing during the loud tone at the end (7.7 s onwards)


def test_audio_that_starts_late_stays_aligned_to_the_video(tmp_path):
    clip = tmp_path / "late audio.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x180:r=25:d=9",
                    "-itsoffset", "2", "-i", str(SPEECH), "-map", "0:v", "-map", "1:a", "-c:v", "libx264",
                    "-c:a", "aac", "-t", "9", str(clip)], check=True)
    found = segments(clip, tmp_path)
    print(f"\n[real vad/late audio] segments={found}")
    assert 1.9 < found[0][0] < 2.4  # speech is heard 2 s into the video, not at 0 s


def test_video_without_audio_is_reported(tmp_path):
    clip = tmp_path / "silent.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x180:r=25:d=1",
                    "-c:v", "libx264", str(clip)], check=True)
    with pytest.raises(MediaError, match="no audio track"):
        extract_speech_audio("ffmpeg", str(clip), str(tmp_path / "x.wav"))


@pytest.fixture
def manager(db):
    m = JobManager(JobStore(db), 1)
    m.register(REFRAME_ANALYZE_JOB, make_reframe_analyze_handler(
        lambda: find_tool("ffmpeg"), lambda p: DetectorSet(red_detector, None, "red test detector", [])))
    yield m
    m.shutdown()


def run(manager, payload):
    job = manager.submit(REFRAME_ANALYZE_JOB, "a", payload)
    assert manager.wait_idle(120)
    return manager.store.get(job.id)


def test_speech_driven_analysis_job_records_speech(manager, tmp_path):
    clip = tmp_path / "talk.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=320x180:r=25:d=6.2",
                    "-i", str(SPEECH), "-c:v", "libx264", "-c:a", "aac", "-shortest", str(clip)], check=True)
    features = ReframeSettings(tracking_mode="speech_audio", detect_persons=False).analysis_features()
    job = run(manager, {"path": str(clip), "width": 320, "height": 180, "duration": 6.2, "sample_fps": 5,
                        "features": features})
    assert job.state is JobState.COMPLETED, job.error
    analysis = TrackingAnalysis.from_dict(job.result["analysis"])
    assert analysis.has("speech") and analysis.speech.detector.startswith("Silero")
    assert analysis.speech.segments(0.5)
    assert reanalysis_reasons(analysis, ReframeSettings(tracking_mode="speech_audio")) == []
    assert reanalysis_reasons(analysis, ReframeSettings(tracking_mode="active_speaker"))  # no mouth data


def test_mouth_analysis_without_landmarks_is_flagged(manager, tmp_path):
    clip = tmp_path / "square.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x303030:s=320x180:r=25:d=2",
                    "-f", "lavfi", "-i", "color=c=red:s=40x40:r=25:d=2", "-i", str(SPEECH),
                    "-filter_complex", "[0:v][1:v]overlay=x=100:y=60[v]", "-map", "[v]", "-map", "2:a",
                    "-c:v", "libx264", "-c:a", "aac", "-t", "2", str(clip)], check=True)
    job = run(manager, {"path": str(clip), "width": 320, "height": 180, "duration": 2, "sample_fps": 15,
                        "features": ["faces", "mouth", "speech"]})
    assert job.state is JobState.COMPLETED, job.error
    analysis = TrackingAnalysis.from_dict(job.result["analysis"])
    assert "mouth" not in analysis.features
    assert any("Mouth movement could not be measured" in w for w in analysis.warnings)
    assert reanalysis_reasons(analysis, ReframeSettings(tracking_mode="active_speaker"))

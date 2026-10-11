"""Speech-activity data, audio chunking and mouth-movement measurement (fake VAD, synthetic frames)."""

import wave

import pytest

from ai_video_studio.core.errors import JobCancelled
from ai_video_studio.reframe.model import Detection, SpeechActivity
from ai_video_studio.reframe.mouth import MouthMotion, mouth_regions
from ai_video_studio.reframe.speech import BLOCK, CHUNK_BLOCKS, RATE, analyse_speech, speech_audio_args

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")


def write_wav(path, samples, rate=16000):
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())


def test_speech_activity_helpers():
    sp = SpeechActivity(10, [0.0] * 10 + [0.9] * 20 + [0.1] * 2 + [0.9] * 10 + [0.0] * 10, [0.5] * 52)
    assert sp.duration == pytest.approx(5.2)
    assert sp.prob_at(1.5) == 0.9 and sp.prob_at(-1) == 0.0 and sp.prob_at(99) == 0.0
    assert sp.speech_fraction(1.0, 3.0, 0.5) == pytest.approx(1.0)
    assert sp.speech_fraction(0.0, 2.0, 0.5) == pytest.approx(0.5)
    assert sp.segments(0.5) == [(1.0, 4.2)]  # the 0.2 s dip is bridged
    assert SpeechActivity.from_dict(sp.to_dict()).segments(0.5) == [(1.0, 4.2)]


def test_analyse_speech_processes_long_audio_in_chunks(tmp_path):
    seconds = 70  # more than two 30-second chunks
    t = np.arange(seconds * 16000) / 16000
    samples = np.where((t > 10) & (t < 20), 0.3 * np.sin(2 * np.pi * 220 * t), 0.0).astype(np.float32)
    path = tmp_path / "a.wav"
    write_wav(path, samples)
    calls = []

    def fake_vad(chunk):
        calls.append(len(chunk))
        assert len(chunk) % BLOCK == 0 and len(chunk) <= CHUNK_BLOCKS * BLOCK
        rms = np.sqrt((chunk.reshape(-1, BLOCK) ** 2).mean(axis=1))
        return (rms > 0.05).astype(np.float32)

    progress = []
    sp = analyse_speech(str(path), vad=fake_vad, detector="fake", progress=progress.append)
    assert len(calls) == 3 and progress[-1] == pytest.approx(1.0)
    assert sp.rate == RATE and sp.duration == pytest.approx(seconds, abs=0.05)
    assert sp.segments(0.5) == [(pytest.approx(10.0, abs=0.05), pytest.approx(20.0, abs=0.05))]
    assert sp.energy_at(15) > 0.6 and sp.energy_at(5) == 0.0


def test_analyse_speech_can_be_cancelled(tmp_path):
    path = tmp_path / "a.wav"
    write_wav(path, np.zeros(16000 * 70, np.float32))

    def cancel():
        raise JobCancelled("Cancelled by user")

    with pytest.raises(JobCancelled):
        analyse_speech(str(path), vad=lambda c: np.zeros(len(c) // BLOCK), check_cancelled=cancel)


def test_audio_extraction_aligns_audio_to_video_time_zero():
    args = speech_audio_args("ffmpeg", "in.mp4", "out.wav")
    assert "aresample=async=1:first_pts=0" in args and args[args.index("-ar") + 1] == "16000"


def face_with_landmarks(x=100, y=60, size=80, frame=(320, 240)):
    w, h = frame
    pts = [x + 25, y + 30, x + 55, y + 30, x + 40, y + 45, x + 28, y + 60, x + 52, y + 60]
    norm = tuple(v / (w if i % 2 == 0 else h) for i, v in enumerate(pts))
    return Detection(x / w, y / h, size / w, size / h, 0.9, "face", landmarks=norm)


def frame_with_mouth(opening, shift=0, size=(320, 240)):
    import cv2

    rng = np.random.default_rng(1)
    img = (rng.random((size[1], size[0])) * 40 + 100).astype(np.uint8)
    cv2.circle(img, (140 + shift, 90), 40, 170, -1)              # face
    cv2.circle(img, (125 + shift, 90), 5, 30, -1)                # eyes
    cv2.circle(img, (155 + shift, 90), 5, 30, -1)
    cv2.ellipse(img, (140 + shift, 121), (12, 1 + opening), 0, 0, 360, 20, -1)  # mouth
    return img


def test_mouth_motion_rises_when_the_mouth_moves_not_when_it_is_still():
    det = face_with_landmarks()
    assert mouth_regions(Detection(0.1, 0.1, 0.2, 0.2, 0.9), 320, 240) is None  # no landmarks (Haar)
    still, talking = MouthMotion(), MouthMotion()
    still_values, talk_values = [], []
    for i in range(10):
        still_values.append(still.measure(1, i, frame_with_mouth(2), det))
        talk_values.append(talking.measure(1, i, frame_with_mouth(8 if i % 2 else 1), det))
    assert still_values[0] is None and talk_values[0] is None   # nothing to compare with yet
    assert max(still_values[1:]) < 0.05
    assert min(talk_values[1:]) > 0.2


def test_mouth_motion_ignores_frames_that_are_not_consecutive():
    m = MouthMotion()
    det = face_with_landmarks()
    m.measure(1, 0, frame_with_mouth(2), det)
    assert m.measure(1, 5, frame_with_mouth(8), det) is None


def test_whole_head_movement_is_compensated():
    det = face_with_landmarks()
    m = MouthMotion()
    values = []
    for i in range(8):
        # The whole face (eyes and mouth) shifts by 8 px each frame; the mouth does not open.
        values.append(m.measure(1, i, frame_with_mouth(2, shift=8 if i % 2 else 0), det))
    # Without subtracting the eye region this reads about 0.4 (talking reads about 0.9).
    assert max(values[1:]) < 0.05

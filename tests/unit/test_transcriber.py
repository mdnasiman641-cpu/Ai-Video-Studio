import pytest

from ai_video_studio.captions import transcriber as tr
from ai_video_studio.core.errors import JobCancelled
from tests.fakes import FakeFactory, FakeWhisperModel, fake_segments

SEGS = fake_segments([
    [(0.0, 0.5, " Hello"), (0.6, 1.0, " world.")],
    [(1.2, 1.6, " This"), (1.7, 2.0, " is"), (2.1, 2.6, " a"), (2.7, 3.2, " test.")],
])


@pytest.fixture(autouse=True)
def clear_cache():
    tr.clear_model_cache()
    yield
    tr.clear_model_cache()


def test_device_attempts():
    assert tr.device_attempts("cpu", 1) == [("cpu", "int8")]
    assert tr.device_attempts("auto", 0) == [("cpu", "int8")]
    assert tr.device_attempts("auto", 1) == [("cuda", "float16"), ("cpu", "int8")]
    assert tr.device_attempts("cuda", 0)[0][0] == "cuda"


def test_options_validation(tmp_path):
    tr.TranscriptionOptions(language="bn").validate()
    with pytest.raises(tr.TranscriptionError):
        tr.TranscriptionOptions(language="xx").validate()
    with pytest.raises(tr.TranscriptionError):
        tr.TranscriptionOptions(model="giant").validate()
    with pytest.raises(tr.TranscriptionError):
        tr.TranscriptionOptions(model_path=str(tmp_path / "nope")).validate()
    tr.TranscriptionOptions(model="ignored", model_path=str(tmp_path)).validate()


def test_cpu_transcription_with_progress():
    model = FakeWhisperModel(SEGS)
    factory = FakeFactory(model)
    progress = []
    result = tr.transcribe_file("a.wav", tr.TranscriptionOptions(language="en", max_chars=12), progress.append,
                                model_factory=factory, cuda_devices=0)
    assert factory.loads == [("small", "cpu", "int8")]
    assert result.device == "cpu" and result.language == "en" and not result.warnings
    assert [s.text for s in result.track.segments] == ["Hello world.", "This is a", "test."]
    assert progress[-1] == 1.0
    kwargs = model.calls[0][1]
    assert kwargs["language"] == "en" and kwargs["word_timestamps"] is True


def test_auto_language_passes_none():
    model = FakeWhisperModel(SEGS, language="bn")
    result = tr.transcribe_file("a.wav", tr.TranscriptionOptions(language="auto"), model_factory=FakeFactory(model),
                                cuda_devices=0)
    assert model.calls[0][1]["language"] is None and result.language == "bn"


def test_cuda_load_failure_falls_back_to_cpu():
    factory = FakeFactory(FakeWhisperModel(SEGS), fail_devices={"cuda"})
    result = tr.transcribe_file("a.wav", tr.TranscriptionOptions(), model_factory=factory, cuda_devices=1)
    assert [d for _, d, _ in factory.loads] == ["cuda", "cpu"]
    assert result.device == "cpu" and "falling back to CPU" in result.warnings[0]


def test_cuda_runtime_failure_retries_on_cpu():
    class Factory(FakeFactory):
        def __call__(self, name, device, compute_type, root):
            self.loads.append(device)
            if device == "cuda":
                return FakeWhisperModel(SEGS, fail_on_transcribe=RuntimeError("cuDNN failed"))
            return FakeWhisperModel(SEGS)

    factory = Factory(None)
    result = tr.transcribe_file("a.wav", tr.TranscriptionOptions(), model_factory=factory, cuda_devices=1)
    assert factory.loads == ["cuda", "cpu"] and result.device == "cpu"


def test_cpu_load_failure_is_reported_clearly():
    factory = FakeFactory(None, fail_devices={"cpu"})
    factory.fail_devices = {"cpu"}

    def failing(name, device, compute, root):
        raise OSError("ProxyError 403 Forbidden")

    with pytest.raises(tr.TranscriptionError, match="Could not download"):
        tr.transcribe_file("a.wav", tr.TranscriptionOptions(), model_factory=failing, cuda_devices=0)


def test_cancellation_between_segments():
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        if calls["n"] > 2:
            raise JobCancelled("Cancelled by user")

    with pytest.raises(JobCancelled):
        tr.transcribe_file("a.wav", tr.TranscriptionOptions(), check_cancelled=check,
                           model_factory=FakeFactory(FakeWhisperModel(SEGS)), cuda_devices=0)


def test_no_speech_warning_and_model_cache():
    factory = FakeFactory(FakeWhisperModel([], duration=5))
    result = tr.transcribe_file("a.wav", tr.TranscriptionOptions(), model_factory=factory, cuda_devices=0)
    assert result.track.segments == [] and "No speech" in result.warnings[0]
    tr.transcribe_file("b.wav", tr.TranscriptionOptions(), model_factory=factory, cuda_devices=0)
    assert len(factory.loads) == 1  # model reused between jobs

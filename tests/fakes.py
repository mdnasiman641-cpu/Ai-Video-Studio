"""Test doubles for faster-whisper (no model download needed)."""

from types import SimpleNamespace


def fake_segments(words_per_segment):
    """[[(start, end, word), ...], ...] -> faster-whisper-like segment objects."""
    segments = []
    for words in words_per_segment:
        ws = [SimpleNamespace(start=s, end=e, word=w) for s, e, w in words]
        segments.append(SimpleNamespace(start=ws[0].start, end=ws[-1].end, text="".join(w.word for w in ws), words=ws))
    return segments


class FakeWhisperModel:
    def __init__(self, segments, language="en", duration=None, fail_on_transcribe=None):
        self.segments = segments
        self.language = language
        self.duration = duration if duration is not None else (segments[-1].end if segments else 0)
        self.fail_on_transcribe = fail_on_transcribe
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        if self.fail_on_transcribe:
            raise self.fail_on_transcribe
        info = SimpleNamespace(language=kwargs.get("language") or self.language, language_probability=0.97,
                               duration=self.duration)
        return iter(self.segments), info


class FakeFactory:
    """Records (model, device, compute_type) loads; can fail for specific devices."""

    def __init__(self, model, fail_devices=()):
        self.model = model
        self.fail_devices = set(fail_devices)
        self.loads = []

    def __call__(self, name, device, compute_type, download_root):
        self.loads.append((name, device, compute_type))
        if device in self.fail_devices:
            raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
        return self.model

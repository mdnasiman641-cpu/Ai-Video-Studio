"""Platform behaviour that differs on Windows (pythonw, console encodings, credential store)."""

import io
import sys
import uuid

import pytest

from ai_video_studio.app import ensure_std_streams
from ai_video_studio.storage import secrets


def test_missing_std_streams_are_replaced(monkeypatch):
    # pythonw.exe and windowed PyInstaller builds start with sys.stdout/stderr = None.
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    ensure_std_streams()
    sys.stdout.write("progress output must not crash\n")
    sys.stderr.write("বাংলা\n")
    from tqdm import tqdm  # used by huggingface_hub while downloading Whisper models

    assert sum(tqdm(range(3))) == 3


def test_unencodable_output_is_escaped(monkeypatch):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")  # Windows ANSI code page when redirected
    monkeypatch.setattr(sys, "stderr", stream)
    ensure_std_streams()
    sys.stderr.write("ভিডিও.mp4\n")
    sys.stderr.flush()
    assert b"\\u09ad" in raw.getvalue()


@pytest.mark.skipif(not secrets.secure_store_available(), reason="no OS credential store on this machine")
def test_real_credential_store_round_trip():
    provider = f"avs-test-{uuid.uuid4().hex[:8]}"  # never touches the user's real gemini/openai entries
    try:
        secrets.set_api_key(provider, "test-value-123")
        assert secrets.get_api_key(provider) == "test-value-123"
        assert secrets.key_source(provider)
    finally:
        secrets.delete_api_key(provider)
    assert secrets.get_api_key(provider) is None

import json

import pytest

from ai_video_studio.captions.model import CaptionSegment, CaptionTrack
from ai_video_studio.captions.style import BUILTIN_PRESETS, CaptionStyle
from ai_video_studio.core.errors import StudioError
from ai_video_studio.storage import secrets
from ai_video_studio.storage.presets import PresetStore
from ai_video_studio.storage.project import PROJECT_VERSION, Project


def test_presets(db):
    store = PresetStore(db)
    names = store.names()
    assert names[: len(BUILTIN_PRESETS)] == [(n, True) for n in BUILTIN_PRESETS]
    style = CaptionStyle(font_size=99, text_color="#ABCDEF")
    store.save("Mine", style)
    store.save("Mine", CaptionStyle(font_size=50))  # overwrite
    assert store.get("Mine").font_size == 50 and ("Mine", False) in store.names()
    with pytest.raises(StudioError):
        store.save("Boxed", style)
    with pytest.raises(StudioError):
        store.save("  ", style)
    with pytest.raises(StudioError):
        store.delete("Boxed")
    with pytest.raises(Exception):
        store.save("Bad", CaptionStyle(font_size=1))
    store.delete("Mine")
    assert not store.exists("Mine")
    with pytest.raises(StudioError):
        store.get("Mine")
    # built-in returned as a copy
    store.get("Boxed").font_size = 1
    assert store.get("Boxed").font_size == BUILTIN_PRESETS["Boxed"].font_size


def test_secrets_env_var(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", " sk-test ")
    assert secrets.get_api_key("openai") == "sk-test"
    assert "OPENAI_API_KEY" in secrets.key_source("openai")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(secrets, "_keyring", lambda: None)
    assert secrets.get_api_key("gemini") is None
    with pytest.raises(StudioError, match="environment variable"):
        secrets.set_api_key("gemini", "abc")


def test_secrets_keyring(monkeypatch):
    store = {}

    class FakeKeyring:
        def get_password(self, app, name):
            return store.get((app, name))

        def set_password(self, app, name, value):
            store[(app, name)] = value

        def delete_password(self, app, name):
            store.pop((app, name))

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(secrets, "_keyring", lambda: FakeKeyring())
    secrets.set_api_key("gemini", " key123 ")
    assert secrets.get_api_key("gemini") == "key123" and secrets.key_source("gemini")
    secrets.delete_api_key("gemini")
    assert secrets.get_api_key("gemini") is None


def test_project_v2_captions_round_trip(tmp_path):
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"\0" * 8)
    project = Project(name="P")
    item = project.add_media(str(clip))
    item.captions = CaptionTrack(segments=[CaptionSegment(0, 1.5, "বাংলা text")], language="bn", source="srt")
    item.caption_style = CaptionStyle(font_size=80, vertical="top")
    path = project.save(tmp_path / "p.avsproj")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == PROJECT_VERSION == 7
    assert "বাংলা" in path.read_text(encoding="utf-8")  # stored as readable UTF-8
    loaded = Project.load(path).media[0]
    assert loaded.captions.to_dict() == item.captions.to_dict()
    assert loaded.caption_style == item.caption_style


def test_project_v1_file_still_loads(tmp_path):
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"\0" * 8)
    v1 = {"format": "ai-video-studio-project", "version": 1, "id": "abc", "name": "Old",
          "media": [{"id": "m1", "path": str(clip), "metadata": None, "error": None}]}
    path = tmp_path / "old.avsproj"
    path.write_text(json.dumps(v1), encoding="utf-8")
    item = Project.load(path).media[0]
    assert item.captions is None and item.caption_style is None
    assert item.effective_caption_style == CaptionStyle()

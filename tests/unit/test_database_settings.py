import pytest

from ai_video_studio.core.errors import SettingsError
from ai_video_studio.storage.database import MIGRATIONS, Database
from ai_video_studio.storage.settings import SETTINGS, SettingsStore


def test_migrations_applied_once(tmp_path):
    path = tmp_path / "a.db"
    db = Database(path)
    assert db.schema_version == len(MIGRATIONS)
    db.close()
    db = Database(path)  # re-opening must not re-run migrations
    assert db.schema_version == len(MIGRATIONS)
    db.close()


def test_settings_defaults_and_persistence(tmp_path):
    db = Database(tmp_path / "s.db")
    store = SettingsStore(db)
    assert store.get("max_concurrent_jobs") == SETTINGS["max_concurrent_jobs"].default
    store.set("max_concurrent_jobs", "3")
    db.close()
    store = SettingsStore(Database(tmp_path / "s.db"))
    assert store.get("max_concurrent_jobs") == 3


def test_settings_validation(db):
    store = SettingsStore(db)
    with pytest.raises(SettingsError):
        store.set("max_concurrent_jobs", 99)
    with pytest.raises(SettingsError):
        store.set("max_concurrent_jobs", "abc")
    with pytest.raises(SettingsError):
        store.set("log_level", "VERBOSE")
    with pytest.raises(SettingsError):
        store.set("ffmpeg_dir", "/definitely/not/a/folder")
    with pytest.raises(SettingsError):
        store.get("no_such_setting")


def test_settings_update_is_all_or_nothing(db):
    store = SettingsStore(db)
    with pytest.raises(SettingsError):
        store.update({"max_concurrent_jobs": 3, "preview_volume": 500})
    assert store.get("max_concurrent_jobs") == SETTINGS["max_concurrent_jobs"].default


def test_settings_reset_and_listener(db):
    store = SettingsStore(db)
    seen = []
    store.subscribe(lambda k, v: seen.append((k, v)))
    store.set("preview_volume", 10)
    store.reset("preview_volume")
    assert store.get("preview_volume") == SETTINGS["preview_volume"].default
    assert seen == [("preview_volume", 10), ("preview_volume", SETTINGS["preview_volume"].default)]


def test_bool_setting_coercion(db):
    store = SettingsStore(db)
    store.set("reopen_last_project", "false")
    assert store.get("reopen_last_project") is False

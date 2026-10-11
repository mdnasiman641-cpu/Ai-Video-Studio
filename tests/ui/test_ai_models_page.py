"""GUI tests for the AI Models page and the title suggestion flow (mocked providers, no network)."""

import json

import pytest
from PySide6.QtWidgets import QMessageBox

from ai_video_studio.captions.model import CaptionSegment, CaptionTrack
from ai_video_studio.jobs.ai_jobs import AI_TEST_JOB, AI_TITLES_JOB, make_ai_test_handler, make_ai_titles_handler
from ai_video_studio.jobs.models import JobState
from ai_video_studio.services import Services
from ai_video_studio.storage import secrets
from ai_video_studio.ui import caption_dialogs
from ai_video_studio.ui.main_window import MainWindow
from ai_video_studio.ui.pages.ai_models import ProfileDialog
from tests.conftest import requires_ffmpeg

KEY = "sk-ui-test-1234567890abcdef"


class FakeKeyring:
    def __init__(self):
        self.stored = {}

    def get_password(self, app, name):
        return self.stored.get(name)

    def set_password(self, app, name, value):
        self.stored[name] = value

    def delete_password(self, app, name):
        self.stored.pop(name, None)


def transport(url, headers, body, timeout):
    if body is None:  # connection test
        return 200, {"data": [{"id": "my-model"}, {"id": "other"}]}
    if "titles" in json.dumps(body) or "title" in json.dumps(body):
        answer = {"title": "Generated title", "description": "Short description.", "hashtags": ["#one"]}
    else:
        answer = {"ok": True}
    return 200, {"choices": [{"message": {"content": json.dumps(answer)}}], "usage": {"prompt_tokens": 9,
                                                                                       "completion_tokens": 3}}


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    keyring = FakeKeyring()
    monkeypatch.setattr(secrets, "_keyring", lambda: keyring)
    monkeypatch.setattr(secrets, "secure_store_available", lambda: True)
    for name in ("warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    services = Services(tmp_path / "ui.db")
    services.jobs.register(AI_TEST_JOB, make_ai_test_handler(secrets.get_api_key, transport=transport,
                                                             usage_log=services.ai_usage))
    services.jobs.register(AI_TITLES_JOB, make_ai_titles_handler(secrets.get_api_key, transport=transport,
                                                                 usage_log=services.ai_usage))
    win = MainWindow(services)
    win.show()
    win.navigate("AI Models")
    win.keyring = keyring
    yield win
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Discard)
    win.close()
    win.deleteLater()
    services.close()


def add_custom(page, name="My Server", key=KEY):
    dialog = ProfileDialog(page, None, None)
    dialog.name.setText(name)
    dialog.type.setCurrentIndex(dialog.type.findData("openai"))
    dialog.model.setText("my-model")
    dialog.key.setText(key)
    assert not dialog.caps["video"].isEnabled() and not dialog.caps["audio"].isEnabled()
    dialog.accept()
    assert dialog.result() == ProfileDialog.DialogCode.Accepted
    assert page._save_from_dialog(dialog)
    return next(p for p in page.profiles() if p.display_name == name)


def row_of(page, name):
    return next(r for r in range(page.table.rowCount()) if page.table.item(r, 1).text() == name)


def test_page_lists_standard_profiles_and_routing(window):
    page = window.ai_page
    assert window.pages.currentWidget() is page
    assert [page.table.item(r, 1).text() for r in range(page.table.rowCount())] == ["Google Gemini", "OpenAI"]
    assert "not available" in page.routing.text()


def test_add_enable_reorder_test_and_delete(qtbot, window, tmp_path):
    page = window.ai_page
    profile = add_custom(page)
    assert window.keyring.stored[profile.id] == KEY
    row = row_of(page, "My Server")
    assert page.table.item(row, 6).text().startswith("Set (") and KEY not in page.table.item(row, 6).text()
    assert KEY not in (tmp_path / "ui.db").read_bytes().decode("latin-1")  # never in the database
    page.table.selectRow(row)
    page.set_primary()
    assert page.profiles()[0].id == profile.id and "My Server (my-model)" in page.routing.text()
    page.table.selectRow(row_of(page, "Google Gemini"))
    page.toggle_enabled()
    assert window.services.ai_providers.get("gemini").enabled
    page.table.selectRow(row_of(page, "My Server"))
    job = page.test("connection")
    qtbot.waitUntil(lambda: window.services.jobs.store.get(job.id).state.is_finished, timeout=10000)
    assert window.services.jobs.store.get(job.id).state is JobState.COMPLETED
    qtbot.waitUntil(lambda: page.table.item(row_of(page, "My Server"), 7).text().startswith("✔"), timeout=5000)
    job = page.test("model")
    qtbot.waitUntil(lambda: window.services.jobs.store.get(job.id).state.is_finished, timeout=10000)
    page.refresh_usage()
    assert page.history.rowCount() == 1 and "9+3 (provider)" in page.history.item(0, 6).text()
    assert page.totals.item(0, 6).text() == "unknown"  # no prices entered: cost is not invented
    page.table.selectRow(row_of(page, "My Server"))
    page.delete_profile()
    assert profile.id not in window.keyring.stored and len(page.profiles()) == 2


@requires_ffmpeg
def test_title_suggestion_from_captions(qtbot, window, sample_videos, monkeypatch):
    page = window.ai_page
    profile = add_custom(page)
    window.media_page.import_paths([str(sample_videos["landscape"])])
    qtbot.waitUntil(lambda: window.controller.project.media[0].metadata is not None, timeout=20000)
    item = window.controller.project.media[0]
    window.controller.set_captions(item.id, CaptionTrack([CaptionSegment(0, 1, "We talk about video editing.")]))
    shown = []
    monkeypatch.setattr(caption_dialogs.TitleSuggestionsDialog, "exec", lambda self: shown.append(self.text()))
    window.navigate("Captions")
    captions = window.captions_page
    captions._update_buttons()
    assert captions.titles_button.isEnabled()
    job = captions.suggest_titles()
    qtbot.waitUntil(lambda: bool(shown), timeout=10000)
    assert shown[0].startswith("Generated title") and "#one" in shown[0]
    assert window.services.jobs.store.get(job.id).payload["profiles"][0]["id"] == profile.id
    assert item.captions.segments[0].text == "We talk about video editing."  # nothing was changed

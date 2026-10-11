"""AI provider manager: profiles, keys, the HTTP client, routing, retries, fallback and usage.

Every provider is mocked through an injected transport; no real API is called.
"""

import json
import logging
import sqlite3
import threading

import pytest

from ai_video_studio.ai.client import ProviderClient, provider_usage, scrub
from ai_video_studio.ai.profiles import (
    MEDIA_NOTE,
    ProfileError,
    ProviderProfile,
    ProviderStore,
    default_profiles,
)
from ai_video_studio.ai.providers import AIError
from ai_video_studio.ai.router import AIRouter, RateLimiter, compute_usage
from ai_video_studio.ai.usage import UsageLog
from ai_video_studio.core.errors import JobCancelled
from ai_video_studio.core.logging_setup import JsonFormatter
from ai_video_studio.jobs.ai_jobs import AI_TEST_JOB, AI_TITLES_JOB, make_ai_test_handler, make_ai_titles_handler
from ai_video_studio.jobs.caption_jobs import CORRECTION_JOB, make_correction_handler
from ai_video_studio.jobs.manager import JobManager
from ai_video_studio.jobs.models import JobState
from ai_video_studio.jobs.store import JobStore
from ai_video_studio.storage import secrets
from ai_video_studio.storage.database import MIGRATIONS, Database

KEY = "sk-live-0123456789abcdefSECRET"


def gem(name="Gemini A", **kw):
    kw.setdefault("capabilities", ["text", "json", "caption_correction", "titles"])
    return ProviderProfile(name, "gemini", "gemini-test", id=kw.pop("id", name.lower().replace(" ", "-")), **kw)


def oai(name="OpenAI B", **kw):
    kw.setdefault("capabilities", ["text", "json", "caption_correction", "titles"])
    return ProviderProfile(name, "openai", "gpt-test", id=kw.pop("id", name.lower().replace(" ", "-")), **kw)


def gemini_answer(obj, usage=True):
    payload = {"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]}}]}
    if usage:
        payload["usageMetadata"] = {"promptTokenCount": 11, "candidatesTokenCount": 7}
    return 200, payload


def openai_answer(obj, usage=True):
    payload = {"choices": [{"message": {"content": json.dumps(obj)}}]}
    if usage:
        payload["usage"] = {"prompt_tokens": 20, "completion_tokens": 5}
    return 200, payload


class Transport:
    """Answers by URL fragment; records every request."""

    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, body, timeout))
        for fragment, answers in self.routes.items():
            if fragment in url:
                item = answers.pop(0) if len(answers) > 1 else answers[0]
                if isinstance(item, Exception):
                    raise item
                return item
        raise AssertionError(f"unexpected URL {url}")

    def count(self, fragment):
        return sum(1 for c in self.calls if fragment in c[0])


def router(profiles, transport, **kw):
    return AIRouter(profiles, lambda p: KEY, transport=transport, sleep=kw.pop("sleep", lambda s: None), **kw)


# -- profiles and store ------------------------------------------------------------------

def test_profile_validation():
    gem().validate()
    ProviderProfile("Local", "openai_compatible", "llama3", base_url="http://localhost:11434/v1",
                    key_required=False).validate()
    bad = [
        dict(display_name=""), dict(provider_type="claude"), dict(model="has space"),
        dict(base_url="http://example.com/v1"), dict(base_url="ftp://x"), dict(capabilities=["text", "video"]),
        dict(capabilities=["json"]), dict(timeout=1), dict(max_retries=11), dict(key_env="BAD-NAME"),
        dict(cost_input_per_million=-2), dict(key_required=False),
    ]
    for change in bad:
        data = gem().to_dict() | change
        with pytest.raises(ProfileError):
            ProviderProfile.from_dict(data).validate()
    with pytest.raises(ProfileError, match="never sends audio or video"):
        ProviderProfile.from_dict(gem().to_dict() | {"capabilities": ["text", "audio"]}).validate()
    assert not gem(capabilities=["text"]).supports("audio")


def test_store_crud_order_and_persistence(tmp_path):
    db = Database(tmp_path / "ai.db")
    store = ProviderStore(db)
    store.ensure_defaults()
    assert [p.id for p in store.list()] == ["gemini", "openai"] and not any(p.enabled for p in store.list())
    local = ProviderProfile("Local", "openai_compatible", "llama3", base_url="http://127.0.0.1:8080/v1",
                            key_required=False)
    store.save(local)
    assert [p.id for p in store.list()][-1] == local.id
    store.set_primary(local.id)
    assert store.list()[0].id == local.id
    store.move(local.id, 1)
    assert [p.id for p in store.list()][1] == local.id
    with pytest.raises(ProfileError):
        store.save(ProviderProfile("local", "openai_compatible", "x", base_url="http://localhost/v1",
                                   key_required=False))  # same name, different profile
    with pytest.raises(ProfileError):
        store.reorder(["gemini"])
    db.close()
    again = ProviderStore(Database(tmp_path / "ai.db"))
    assert len(again.list()) == 3
    again.delete(local.id)
    again.restore_defaults()
    assert [p.id for p in again.list()] == ["gemini", "openai"]


def test_first_start_imports_earlier_ai_settings(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("".join(MIGRATIONS[:3]) + "PRAGMA user_version = 3;")
    for key, value in (("ai_correction_provider", "openai"), ("openai_model", "gpt-old"), ("ai_timeout_seconds", 90)):
        conn.execute("INSERT INTO settings(key, value) VALUES(?, ?)", (key, json.dumps(value)))
    conn.commit()
    conn.close()
    store = ProviderStore(Database(path))
    store.ensure_defaults()
    first = store.list()[0]
    assert (first.id, first.model, first.enabled, first.timeout) == ("openai", "gpt-old", True, 90)
    assert not store.list()[1].enabled
    assert default_profiles()[0].id == "gemini"


def test_profile_keys_env_then_store(monkeypatch):
    stored = {}

    class FakeKeyring:
        def get_password(self, app, name):
            return stored.get(name)

        def set_password(self, app, name, value):
            stored[name] = value

        def delete_password(self, app, name):
            stored.pop(name)

    monkeypatch.setattr(secrets, "_keyring", lambda: FakeKeyring())
    secrets.set_api_key("profile123", "from-store")
    assert secrets.profile_key("profile123") == "from-store"
    monkeypatch.setenv("MY_KEY", "from-env")
    assert secrets.profile_key("profile123", "MY_KEY") == "from-env"
    assert "MY_KEY" in secrets.profile_key_source("profile123", "MY_KEY")
    secrets.delete_api_key("profile123")
    assert secrets.profile_key("profile123") is None


# -- client -----------------------------------------------------------------------------

def test_gemini_and_openai_request_shapes():
    t = Transport({":generateContent": [gemini_answer({"ok": True})],
                   "/chat/completions": [openai_answer({"ok": True})]})
    data, usage = ProviderClient(gem(), KEY, t).chat_json("sys", "user")
    url, headers, body, timeout = t.calls[0]
    assert url == "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent"
    assert headers == {"x-goog-api-key": KEY} and KEY not in url
    assert body["generationConfig"]["responseMimeType"] == "application/json" and timeout == 60
    assert data == {"ok": True} and usage == {"input": 11, "output": 7}
    custom = ProviderProfile("Local", "openai_compatible", "llama3", base_url="http://localhost:1234/v1/",
                             key_required=False, capabilities=["text", "caption_correction"])
    ProviderClient(custom, None, t).chat_json("s", "u")
    url, headers, body, _ = t.calls[-1]
    assert url == "http://localhost:1234/v1/chat/completions" and headers == {}
    assert "response_format" not in body  # no native JSON mode declared: none requested


def test_missing_key_and_redaction_of_keys_in_errors(caplog):
    with pytest.raises(AIError) as err:
        ProviderClient(gem(), None, Transport({}))
    assert err.value.kind == "not_configured"
    leak = {"error": {"message": f"Incorrect API key provided: {KEY[:10]}...{KEY[-12:]}"}}
    t = Transport({"/chat/completions": [(401, leak)]})
    with pytest.raises(AIError) as err:
        ProviderClient(oai(), KEY, t).chat_json("s", "u")
    assert err.value.kind == "auth" and KEY[-12:] not in str(err.value) and KEY[:10] not in str(err.value)
    assert scrub(f"key={KEY}", KEY) == "key=***"
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "Authorization: Bearer %s", (KEY,), None)
    assert KEY not in JsonFormatter().format(record)


def test_invalid_json_and_usage_parsing():
    t = Transport({":generateContent": [(200, {"candidates": [{"content": {"parts": [{"text": "not json"}]}}]})]})
    with pytest.raises(AIError) as err:
        ProviderClient(gem(), KEY, t).chat_json("s", "u")
    assert err.value.kind == "invalid_response"
    assert provider_usage("openai", {"usage": {"prompt_tokens": 3, "completion_tokens": 4}}) == {"input": 3,
                                                                                                    "output": 4}
    assert provider_usage("gemini", {}) == {}
    estimated = compute_usage(gem(), {}, "x" * 400, "y" * 40)
    assert (estimated.input_tokens, estimated.output_tokens, estimated.source) == (100, 10, "estimated")
    assert estimated.cost is None and estimated.cost_source == "unknown"
    priced = compute_usage(gem(cost_input_per_million=1.0, cost_output_per_million=2.0), {"input": 1000,
                                                                                           "output": 500}, "", "")
    assert priced.cost == pytest.approx(0.002) and priced.source == "provider"
    assert priced.cost_source == "estimated from your prices"


# -- routing ----------------------------------------------------------------------------

def test_timeout_is_retried_with_backoff_then_succeeds():
    sleeps = []
    t = Transport({":generateContent": [AIError("slow", "timeout"), gemini_answer({"a": 1})]})
    result = router([gem(max_retries=2)], t, sleep=sleeps.append).request_json("caption_correction", "s", "u")
    assert result.data == {"a": 1} and t.count("generateContent") == 2 and sleeps == [2.0]
    assert any("retrying" in n for n in result.notes)


def test_rate_limit_retries_are_bounded_then_fallback():
    t = Transport({":generateContent": [(429, {})], "/chat/completions": [openai_answer({"b": 2})]})
    result = router([gem(max_retries=2), oai()], t).request_json("caption_correction", "s", "u")
    assert t.count("generateContent") == 3  # first try + 2 retries, then the fallback
    assert result.profile_name == "OpenAI B" and "Used fallback provider OpenAI B." in result.notes


def test_auth_errors_fall_back_without_retry_and_404_too():
    t = Transport({":generateContent": [(401, {})], "api.openai.com": [(404, {"error": {"message": "no model"}})]})
    third = ProviderProfile("Local", "openai_compatible", "llama3", id="local", base_url="http://localhost:9/v1",
                            key_required=False, capabilities=["text", "caption_correction"])
    t.routes["localhost:9"] = [openai_answer({"c": 3})]
    result = router([gem(max_retries=3), oai(max_retries=3), third], t).request_json("caption_correction", "s", "u")
    assert t.count("generateContent") == 1 and t.count("api.openai.com") == 1 and result.profile_id == "local"


def test_bad_request_is_not_sent_to_other_providers():
    t = Transport({":generateContent": [(400, {"error": {"message": "too long"}})],
                   "/chat/completions": [openai_answer({})]})
    with pytest.raises(AIError) as err:
        router([gem(), oai()], t).request_json("caption_correction", "s", "u")
    assert err.value.kind == "bad_request" and "not sent to other providers" in str(err.value)
    assert t.count("chat/completions") == 0


def test_invalid_answer_gets_one_retry_then_fallback():
    broken = (200, {"candidates": [{"content": {"parts": [{"text": "oops"}]}}]})
    t = Transport({":generateContent": [broken], "/chat/completions": [openai_answer({"ok": 1})]})
    result = router([gem(max_retries=5), oai()], t).request_json("caption_correction", "s", "u")
    assert t.count("generateContent") == 2 and result.profile_name == "OpenAI B"


def test_all_failures_are_reported_and_no_provider_is_tried_twice():
    t = Transport({":generateContent": [(503, {})], "/chat/completions": [AIError("down", "network")]})
    with pytest.raises(AIError) as err:
        router([gem(max_retries=1), oai(max_retries=1)], t).request_json("caption_correction", "s", "u")
    assert "Gemini A" in str(err.value) and "OpenAI B" in str(err.value)
    assert t.count("generateContent") == 2 and t.count("chat/completions") == 2


def test_capabilities_keys_and_disabled_profiles_decide_the_plan():
    r = AIRouter([gem(enabled=False), oai(capabilities=["text"]), gem("Gemini C", id="c")],
                 lambda p: None if p.id == "c" else KEY, transport=Transport({}))
    usable, skipped = r.plan("caption_correction")
    assert usable == [] and len(skipped) == 3
    assert any("disabled" in s for s in skipped) and any("no API key" in s for s in skipped)
    with pytest.raises(AIError) as err:
        r.request_json("caption_correction", "s", "u")
    assert err.value.kind == "not_configured" and "AI Models page" in str(err.value)
    with pytest.raises(AIError) as err:
        r.request_json("video", "s", "u")
    assert err.value.kind == "unsupported" and MEDIA_NOTE in str(err.value)


def test_cancellation_stops_retries():
    cancel = threading.Event()

    def sleep(seconds):
        cancel.set()

    t = Transport({":generateContent": [(500, {})]})
    with pytest.raises(JobCancelled):
        router([gem(max_retries=5)], t, sleep=sleep, cancel_event=cancel).request_json("titles", "s", "u")
    assert t.count("generateContent") == 1


def test_local_rate_limit_moves_on_when_the_wait_is_too_long():
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    t = Transport({":generateContent": [gemini_answer({"x": 1})], "/chat/completions": [openai_answer({"y": 2})]})
    profiles = [gem(rate_limit_per_minute=1), oai()]
    r = router(profiles, t, limiter=limiter)
    assert r.request_json("titles", "s", "u").profile_name == "Gemini A"
    now[0] = 5.0
    assert r.request_json("titles", "s", "u").profile_name == "OpenAI B"  # 55 s wait > 30 s: fallback


def test_usage_log_records_without_content(db):
    usage = UsageLog(db)
    t = Transport({":generateContent": [gemini_answer({"secret answer": "x"})]})
    router([gem(cost_input_per_million=1, cost_output_per_million=1)], t, usage_log=usage).request_json(
        "caption_correction", "PROMPT TEXT", "CAPTION TEXT")
    t2 = Transport({"/chat/completions": [(401, {})]})
    with pytest.raises(AIError):
        router([oai()], t2, usage_log=usage).request_json("caption_correction", "s", "u")
    rows = usage.recent()
    assert [r["outcome"] for r in rows] == ["error", "success"]
    assert rows[1]["usage_source"] == "provider" and rows[1]["input_tokens"] == 11 and rows[1]["cost"] > 0
    assert rows[0]["error_kind"] == "auth" and rows[0]["status_code"] == 401
    dump = json.dumps(rows)
    assert "PROMPT TEXT" not in dump and "CAPTION TEXT" not in dump and "secret answer" not in dump
    totals = usage.totals()
    assert {t["provider_name"] for t in totals} == {"Gemini A", "OpenAI B"}


# -- jobs ---------------------------------------------------------------------------------

def run(manager, kind, payload):
    job = manager.submit(kind, kind, payload)
    assert manager.wait_idle(10)
    return manager.store.get(job.id)


def test_correction_job_uses_the_profiles_in_its_payload_and_old_payloads_still_work(db):
    segs = [{"id": "s1", "text": "helo world"}]
    t = Transport({":generateContent": [(503, {})],
                   "/chat/completions": [openai_answer({"corrections": [{"id": "s1", "text": "hello world"}]})]})
    manager = JobManager(JobStore(db), 1)
    manager.register(CORRECTION_JOB, make_correction_handler(lambda account: KEY, transport=t, sleep=lambda s: None))
    job = run(manager, CORRECTION_JOB, {"media_id": "m", "profiles": [gem(max_retries=0).to_dict(), oai().to_dict()],
                                        "segments": segs, "language": "en"})
    assert job.state is JobState.COMPLETED, job.error
    assert job.result["provider"] == "OpenAI B" and job.result["suggestions"][0]["suggested"] == "hello world"
    assert any("fallback" in n for n in job.result["notes"])
    legacy = Transport({"api.openai.com": [openai_answer({"corrections": []})]})
    manager.register(CORRECTION_JOB, make_correction_handler(lambda account: KEY, transport=legacy))
    job = run(manager, CORRECTION_JOB, {"media_id": "m", "provider": "openai", "model": "gpt", "segments": segs})
    assert job.state is JobState.COMPLETED and job.result["suggestions"] == []
    assert KEY not in json.dumps(manager.store.get(job.id).payload)
    manager.shutdown()


def test_connection_and_model_tests(db):
    t = Transport({"/models?pageSize": [(200, {"models": [{"name": "models/gemini-test"}]})],
                   ":generateContent": [gemini_answer({"ok": True})]})
    usage = UsageLog(db)
    manager = JobManager(JobStore(db), 1)
    manager.register(AI_TEST_JOB, make_ai_test_handler(lambda account: KEY, transport=t, usage_log=usage))
    job = run(manager, AI_TEST_JOB, {"profile": gem().to_dict(), "mode": "connection"})
    assert job.state is JobState.COMPLETED and "1 model(s)" in job.result["message"] and job.result["model_listed"]
    assert t.calls[0][2] is None  # a GET: no prompt is sent for a connection test
    job = run(manager, AI_TEST_JOB, {"profile": gem().to_dict(), "mode": "model"})
    assert job.state is JobState.COMPLETED and "answered correctly" in job.result["message"]
    assert usage.recent()[0]["capability"] == "test"
    manager.register(AI_TEST_JOB, make_ai_test_handler(lambda account: KEY, transport=Transport(
        {"/models": [(401, {"error": {"message": "invalid key"}})]})))
    job = run(manager, AI_TEST_JOB, {"profile": gem().to_dict(), "mode": "connection"})
    assert job.state is JobState.FAILED and "rejected the API key" in job.error
    manager.shutdown()


def test_title_suggestions(db):
    answer = {"title": "  A good title ", "description": "About things.", "hashtags": ["video", "#ai", "", 5]}
    t = Transport({":generateContent": [gemini_answer(answer)]})
    manager = JobManager(JobStore(db), 1)
    manager.register(AI_TITLES_JOB, make_ai_titles_handler(lambda account: KEY, transport=t))
    job = run(manager, AI_TITLES_JOB, {"media_id": "m", "profiles": [gem().to_dict()], "text": "hello " * 5000})
    assert job.state is JobState.COMPLETED, job.error
    assert job.result["title"] == "A good title" and job.result["hashtags"] == ["#video", "#ai"]
    sent = json.loads(t.calls[0][2]["contents"][0]["parts"][0]["text"])
    assert len(sent["text"]) == 12_000  # long caption text is truncated
    manager.shutdown()

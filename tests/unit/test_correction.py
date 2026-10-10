import json

import pytest

from ai_video_studio.ai.providers import AIError, JsonProvider, ProviderConfig, classify_status
from ai_video_studio.captions.correction import (
    apply_suggestions,
    build_user_prompt,
    parse_corrections,
    request_corrections,
    word_diff,
)
from ai_video_studio.captions.model import CaptionSegment

SEGS = [{"id": "a", "text": "helo wrld"}, {"id": "b", "text": "fine"}]


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, url, headers, body, timeout):
        self.requests.append((url, headers, body, timeout))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def gemini_ok(obj):
    return 200, {"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]}}]}


def openai_ok(obj):
    return 200, {"choices": [{"message": {"content": json.dumps(obj)}}]}


def _provider(name, transport, retries=2):
    return JsonProvider(ProviderConfig(name, "test-model", "secret-key", retries=retries), transport,
                        sleep=lambda s: None)


def test_gemini_request_shape():
    t = Transport([gemini_ok({"corrections": [{"id": "a", "text": "hello world"}]})])
    out = request_corrections(_provider("gemini", t), SEGS, "en")
    url, headers, body, _ = t.requests[0]
    assert url.endswith("/models/test-model:generateContent")
    assert headers == {"x-goog-api-key": "secret-key"}
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert json.loads(body["contents"][0]["parts"][0]["text"])["captions"][0] == {"id": "a", "text": "helo wrld"}
    assert out == [{"id": "a", "original": "helo wrld", "suggested": "hello world"}]


def test_openai_request_shape_and_code_fence():
    fenced = (200, {"choices": [{"message": {"content": "```json\n{\"corrections\": []}\n```"}}]})
    t = Transport([fenced])
    assert request_corrections(_provider("openai", t), SEGS, "en") == []
    url, headers, body, _ = t.requests[0]
    assert url == "https://api.openai.com/v1/chat/completions"
    assert headers["Authorization"] == "Bearer secret-key"
    assert body["model"] == "test-model" and body["response_format"] == {"type": "json_object"}


def test_retry_on_rate_limit_then_success():
    t = Transport([(429, {"error": {"message": "slow down"}}), (503, {}), gemini_ok({"corrections": []})])
    assert request_corrections(_provider("gemini", t), SEGS, None) == []
    assert len(t.requests) == 3


def test_retries_exhausted():
    t = Transport([(500, {}), (500, {}), (500, {})])
    with pytest.raises(AIError) as info:
        request_corrections(_provider("gemini", t), SEGS, None)
    assert info.value.kind == "server" and len(t.requests) == 3


@pytest.mark.parametrize("status,kind", [(401, "auth"), (403, "auth"), (400, "bad_request"), (404, "bad_request")])
def test_no_retry_for_auth_and_bad_request(status, kind):
    t = Transport([(status, {"error": {"message": "nope"}})])
    with pytest.raises(AIError) as info:
        request_corrections(_provider("openai", t), SEGS, None)
    assert info.value.kind == kind and len(t.requests) == 1


def test_network_error_retried():
    t = Transport([AIError("down", "network"), openai_ok({"corrections": []})])
    assert request_corrections(_provider("openai", t), SEGS, None) == []


def test_invalid_responses():
    for bad in [(200, {"candidates": []}), (200, {"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}),
                gemini_ok(["list"]), gemini_ok({"nothing": 1})]:
        with pytest.raises(AIError) as info:
            request_corrections(_provider("gemini", Transport([bad]), retries=0), SEGS, None)
        assert info.value.kind == "invalid_response"


def test_not_configured():
    with pytest.raises(AIError, match="No API key"):
        JsonProvider(ProviderConfig("gemini", "m", ""))
    with pytest.raises(AIError):
        JsonProvider(ProviderConfig("other", "m", "k"))


def test_classify_status_messages_do_not_leak_key():
    err = classify_status(401, {"error": {"message": "bad key"}}, "gemini")
    assert "secret" not in str(err) and err.kind == "auth"


def test_parse_corrections_filters():
    response = {"corrections": [
        {"id": "a", "text": "hello world"},
        {"id": "a", "text": "duplicate"},
        {"id": "zzz", "text": "unknown"},
        {"id": "b", "text": "fine"},              # unchanged
        {"id": "b", "text": ""},                  # empty
        {"id": "b", "text": "x" * 500},           # implausibly long
        "junk",
    ]}
    assert parse_corrections(response, SEGS) == [{"id": "a", "original": "helo wrld", "suggested": "hello world"}]


def test_batches(monkeypatch):
    import ai_video_studio.captions.correction as corr

    monkeypatch.setattr(corr, "BATCH_SIZE", 1)
    t = Transport([gemini_ok({"corrections": []}), gemini_ok({"corrections": [{"id": "b", "text": "Fine."}]})])
    progress = []
    out = request_corrections(_provider("gemini", t), SEGS, "en", progress=progress.append)
    assert len(t.requests) == 2 and progress == [0.5, 1.0] and out[0]["id"] == "b"


def test_word_diff_and_apply():
    assert word_diff("helo big wrld", "hello big world") == [
        ("delete", "helo"), ("insert", "hello"), ("equal", "big"), ("delete", "wrld"), ("insert", "world")]
    segs = [CaptionSegment(0, 1, "helo wrld", id="a"), CaptionSegment(1, 2, "edited later", id="b")]
    sugs = [{"id": "a", "original": "helo wrld", "suggested": "hello world"},
            {"id": "b", "original": "fine", "suggested": "Fine."}]
    assert apply_suggestions(segs, sugs, {"a", "b"}) == (1, 1)
    assert segs[0].text == "hello world" and segs[1].text == "edited later"
    assert build_user_prompt(SEGS, None).startswith('{"language": "unknown"')

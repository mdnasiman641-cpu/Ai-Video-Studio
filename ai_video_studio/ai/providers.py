"""Minimal Gemini and OpenAI clients for JSON-returning text prompts.

These call the providers' official REST endpoints with the standard library, so tests
can inject a fake transport and no SDK is required. Phase 7 extends this into a full
provider manager (priorities, fallback, usage tracking).
"""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from ai_video_studio.core.errors import StudioError

log = logging.getLogger(__name__)

PROVIDERS = {"gemini": "Google Gemini", "openai": "OpenAI"}
DEFAULT_MODELS = {"gemini": "gemini-2.5-flash", "openai": "gpt-4.1-mini"}


class AIError(StudioError):
    """Provider failure. ``kind`` is one of: auth, bad_request, rate_limit, server, network,
    timeout, invalid_response, not_configured."""

    RETRYABLE = {"rate_limit", "server", "network", "timeout"}

    def __init__(self, message: str, kind: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.kind in self.RETRYABLE


# transport(url, headers, body_dict, timeout) -> (status_code, response_dict)
Transport = Callable[[str, dict, dict, float], tuple[int, dict]]


def urllib_transport(url: str, headers: dict, body: dict | None, timeout: float) -> tuple[int, dict]:
    """POST ``body`` as JSON, or GET when ``body`` is None."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers},
                                     method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
        except (ValueError, OSError):
            payload = {}
        return exc.code, payload
    except (socket.timeout, TimeoutError) as exc:
        raise AIError("The AI provider did not respond in time.", "timeout") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise AIError("The AI provider did not respond in time.", "timeout") from exc
        raise AIError(f"Could not reach the AI provider: {exc.reason}", "network") from exc
    except json.JSONDecodeError as exc:
        raise AIError("The AI provider returned an unreadable response.", "invalid_response") from exc


def classify_status(status: int, payload: dict, provider: str) -> AIError:
    detail = ""
    if isinstance(payload.get("error"), dict):
        detail = payload["error"].get("message", "")
    name = PROVIDERS.get(provider, provider)
    if status in (401, 403):
        return AIError(f"{name} rejected the API key ({status}). Check the key in Settings. {detail}".strip(),
                       "auth", status)
    if status == 429:
        return AIError(f"{name} rate limit or quota reached. Try again later. {detail}".strip(), "rate_limit", status)
    if status >= 500:
        return AIError(f"{name} server error ({status}). {detail}".strip(), "server", status)
    return AIError(f"{name} rejected the request ({status}): {detail or 'bad request'}", "bad_request", status)


@dataclass
class ProviderConfig:
    provider: str
    model: str
    api_key: str
    timeout: float = 60.0
    retries: int = 2


class JsonProvider:
    def __init__(self, config: ProviderConfig, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        if config.provider not in PROVIDERS:
            raise AIError(f"Unknown AI provider '{config.provider}'", "not_configured")
        if not config.api_key:
            raise AIError(f"No API key is configured for {PROVIDERS[config.provider]}.", "not_configured")
        if not config.model.strip():
            raise AIError("No model name is configured.", "not_configured")
        self.config = config
        self._transport = transport or urllib_transport
        self._sleep = sleep

    def request_json(self, system: str, user: str) -> dict:
        """Send a prompt and return the parsed JSON object from the model's reply.

        Retries only rate-limit, server, network and timeout errors, with exponential
        backoff. Authentication and bad-request errors fail immediately.
        """
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                result = self._once(system, user)
                log.info("AI request succeeded", extra={"provider": self.config.provider, "model": self.config.model,
                                                        "seconds": round(time.monotonic() - started, 2),
                                                        "prompt_chars": len(system) + len(user)})
                return result
            except AIError as exc:
                log.warning("AI request failed", extra={"provider": self.config.provider, "model": self.config.model,
                                                        "kind": exc.kind, "status": exc.status, "attempt": attempt + 1})
                if not exc.retryable or attempt >= self.config.retries:
                    raise
                attempt += 1
                self._sleep(min(30.0, 2.0 ** attempt))

    def _once(self, system: str, user: str) -> dict:
        provider, model = self.config.provider, self.config.model
        if provider == "gemini":
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            headers = {"x-goog-api-key": self.config.api_key}
            body = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
            }
        else:
            url = "https://api.openai.com/v1/chat/completions"
            headers = {"Authorization": f"Bearer {self.config.api_key}"}
            body = {
                "model": model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_object"},
            }
        status, payload = self._transport(url, headers, body, self.config.timeout)
        if status != 200:
            raise classify_status(status, payload, provider)
        try:
            if provider == "gemini":
                text = payload["candidates"][0]["content"]["parts"][0]["text"]
            else:
                text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            reason = ""
            if provider == "gemini" and payload.get("promptFeedback"):
                reason = f" (blocked: {payload['promptFeedback'].get('blockReason', 'unknown')})"
            raise AIError(f"The AI provider returned no answer{reason}.", "invalid_response") from exc
        try:
            data = json.loads(_strip_code_fence(text))
        except json.JSONDecodeError as exc:
            raise AIError("The AI provider's answer was not valid JSON.", "invalid_response") from exc
        if not isinstance(data, dict):
            raise AIError("The AI provider's answer had an unexpected format.", "invalid_response")
        return data


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text

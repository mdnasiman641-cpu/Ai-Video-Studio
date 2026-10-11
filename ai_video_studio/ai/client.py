"""HTTP client for one provider profile: JSON chat requests, usage figures and connection tests.

Uses the providers' REST endpoints with the standard library (injectable transport for tests):
Gemini ``models/{model}:generateContent``; OpenAI and OpenAI-compatible ``chat/completions``.
Only text is ever sent. The API key goes in a header, never in the URL, and is removed from any
error text before it is shown or logged.
"""

from __future__ import annotations

import json

from ai_video_studio.ai.profiles import ProviderProfile
from ai_video_studio.ai.providers import AIError, Transport, _strip_code_fence, classify_status, urllib_transport


def scrub(text: str, secret: str | None) -> str:
    """Remove the key (and long fragments of it) from text returned by a provider."""
    if not secret or not text:
        return text
    text = text.replace(secret, "***")
    if len(secret) >= 12:
        for size in (8, 6):
            for start in range(0, len(secret) - size + 1):
                fragment = secret[start:start + size]
                if fragment in text:
                    text = text.replace(fragment, "***")
    return text


class ProviderClient:
    def __init__(self, profile: ProviderProfile, api_key: str | None, transport: Transport | None = None):
        if profile.key_required and not api_key:
            raise AIError(f"No API key is set for {profile.display_name}.", "not_configured")
        self.profile = profile
        self.api_key = api_key or ""
        self._transport = transport or urllib_transport

    # -- requests -------------------------------------------------------------------------
    def _headers(self) -> dict:
        if self.profile.provider_type == "gemini":
            return {"x-goog-api-key": self.api_key}
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _send(self, url: str, body: dict | None) -> dict:
        try:
            status, payload = self._transport(url, self._headers(), body, float(self.profile.timeout))
        except AIError as exc:
            raise AIError(scrub(str(exc), self.api_key), exc.kind, exc.status) from None
        if status != 200:
            error = classify_status(status, payload if isinstance(payload, dict) else {}, self.profile.display_name)
            raise AIError(scrub(str(error), self.api_key), error.kind, error.status)
        if not isinstance(payload, dict):
            raise AIError("The AI provider returned an unreadable response.", "invalid_response")
        return payload

    def chat_json(self, system: str, user: str) -> tuple[dict, dict]:
        """Send a prompt; returns ``(parsed JSON object, provider usage figures or {})``."""
        p = self.profile
        json_mode = "json" in p.capabilities
        if p.provider_type == "gemini":
            url = f"{p.url}/models/{p.model}:generateContent"
            config = {"temperature": 0}
            if json_mode:
                config["responseMimeType"] = "application/json"
            body = {"systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": config}
        else:
            url = f"{p.url}/chat/completions"
            body = {"model": p.model,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
            if json_mode:
                body["response_format"] = {"type": "json_object"}
        payload = self._send(url, body)
        try:
            if p.provider_type == "gemini":
                text = payload["candidates"][0]["content"]["parts"][0]["text"]
            else:
                text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            reason = ""
            if p.provider_type == "gemini" and payload.get("promptFeedback"):
                reason = f" (blocked: {payload['promptFeedback'].get('blockReason', 'unknown')})"
            raise AIError(f"{p.display_name} returned no answer{reason}.", "invalid_response") from exc
        try:
            data = json.loads(_strip_code_fence(text or ""))
        except json.JSONDecodeError as exc:
            raise AIError(f"{p.display_name}'s answer was not valid JSON.", "invalid_response") from exc
        if not isinstance(data, dict):
            raise AIError(f"{p.display_name}'s answer had an unexpected format.", "invalid_response")
        return data, provider_usage(p.provider_type, payload)

    def list_models(self) -> list[str]:
        """Model ids the key can use (connection test; sends no prompt)."""
        p = self.profile
        if p.provider_type == "gemini":
            payload = self._send(f"{p.url}/models?pageSize=1000", None)
            return [m.get("name", "").removeprefix("models/") for m in payload.get("models", [])]
        payload = self._send(f"{p.url}/models", None)
        return [m.get("id", "") for m in payload.get("data", []) if isinstance(m, dict)]


def provider_usage(provider_type: str, payload: dict) -> dict:
    """Token counts reported by the provider (``{}`` when it reports none)."""
    try:
        if provider_type == "gemini":
            meta = payload.get("usageMetadata") or {}
            if "promptTokenCount" in meta:
                return {"input": int(meta.get("promptTokenCount") or 0),
                        "output": int(meta.get("candidatesTokenCount") or 0)}
        else:
            usage = payload.get("usage") or {}
            if "prompt_tokens" in usage:
                return {"input": int(usage.get("prompt_tokens") or 0), "output": int(usage.get("completion_tokens") or 0)}
    except (TypeError, ValueError):
        pass
    return {}

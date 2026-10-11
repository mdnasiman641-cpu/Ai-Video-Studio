"""Routes an AI request to the first suitable provider profile, with retries and fallback.

* Only enabled profiles that declare the capability and have a key are used, in the user's
  priority order (the first one is the primary, the rest are fallbacks).
* Temporary failures (rate limit, server error, network, timeout) are retried on the same
  provider with exponential backoff, at most ``max_retries`` times.
* Authentication, missing configuration, an unknown model (HTTP 404) or an unusable answer
  move on to the next provider without retrying the same one.
* A request the provider rejected as invalid (HTTP 400/422) is **not** sent to other
  providers: the same content would most likely be rejected again.
* Each provider is tried at most once per request, so providers can never bounce a request
  back and forth. Cancellation is checked before every attempt and during backoff.

Every attempt is written to the usage log (provider, model, outcome, duration, token counts and
where they came from). No prompt or answer text is stored.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from ai_video_studio.ai.client import ProviderClient
from ai_video_studio.ai.profiles import CAPABILITIES, MEDIA_CAPABILITIES, MEDIA_NOTE, ProviderProfile
from ai_video_studio.ai.providers import AIError, Transport
from ai_video_studio.core.errors import JobCancelled

log = logging.getLogger(__name__)

RETRYABLE = {"rate_limit", "server", "network", "timeout"}
KeyLookup = Callable[[ProviderProfile], "str | None"]


@dataclass
class Usage:
    input_tokens: int | None
    output_tokens: int | None
    source: str              # provider | estimated
    cost: float | None
    cost_source: str         # "estimated from your prices" | "unknown"


@dataclass
class AIResult:
    data: dict
    profile_id: str
    profile_name: str
    model: str
    usage: Usage
    notes: list[str] = field(default_factory=list)


def estimate_tokens(text: str) -> int:
    """Rough token estimate (about four characters per token) when the provider reports none."""
    return max(1, math.ceil(len(text) / 4))


def compute_usage(profile: ProviderProfile, reported: dict, prompt: str, answer: str) -> Usage:
    if reported:
        tokens_in, tokens_out, source = reported["input"], reported["output"], "provider"
    else:
        tokens_in, tokens_out, source = estimate_tokens(prompt), estimate_tokens(answer), "estimated"
    if profile.cost_input_per_million is not None and profile.cost_output_per_million is not None:
        cost = (tokens_in * profile.cost_input_per_million + tokens_out * profile.cost_output_per_million) / 1e6
        return Usage(tokens_in, tokens_out, source, round(cost, 6), "estimated from your prices")
    return Usage(tokens_in, tokens_out, source, None, "unknown")


class RateLimiter:
    """Optional local requests-per-minute limit per profile (in this process)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._sent: dict[str, deque] = {}
        self._lock = threading.Lock()

    def wait_time(self, profile: ProviderProfile) -> float:
        limit = int(profile.rate_limit_per_minute)
        if limit <= 0:
            return 0.0
        now = self._clock()
        with self._lock:
            sent = self._sent.setdefault(profile.id, deque())
            while sent and now - sent[0] >= 60:
                sent.popleft()
            return 0.0 if len(sent) < limit else 60 - (now - sent[0])

    def note(self, profile: ProviderProfile) -> None:
        with self._lock:
            self._sent.setdefault(profile.id, deque()).append(self._clock())


_shared_limiter = RateLimiter()


class AIRouter:
    def __init__(self, profiles: list[ProviderProfile], key_lookup: KeyLookup, transport: Transport | None = None,
                 sleep: Callable[[float], None] | None = None, usage_log=None, limiter: RateLimiter | None = None,
                 cancel_event: threading.Event | None = None, max_wait: float = 30.0):
        self.profiles = profiles
        self.key_lookup = key_lookup
        self.transport = transport
        self.cancel_event = cancel_event or threading.Event()
        self._sleep = sleep
        self.usage_log = usage_log
        self.limiter = limiter or _shared_limiter
        self.max_wait = max_wait

    def plan(self, capability: str) -> tuple[list[tuple[ProviderProfile, str | None]], list[str]]:
        """``(usable profiles with keys, reasons why the others are not used)``."""
        if capability in MEDIA_CAPABILITIES:
            raise AIError(MEDIA_NOTE, "unsupported")
        label = CAPABILITIES.get(capability, capability)
        usable, skipped = [], []
        for profile in self.profiles:
            if not profile.enabled:
                skipped.append(f"{profile.display_name}: disabled")
            elif not profile.supports(capability):
                skipped.append(f"{profile.display_name}: does not offer {label.lower()}")
            else:
                key = self.key_lookup(profile)
                if profile.key_required and not key:
                    skipped.append(f"{profile.display_name}: no API key")
                else:
                    usable.append((profile, key))
        return usable, skipped

    def _wait(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self._sleep is not None:
            self._sleep(seconds)
            self._check()
        elif self.cancel_event.wait(seconds):
            raise JobCancelled("Cancelled by user")

    def _check(self) -> None:
        if self.cancel_event.is_set():
            raise JobCancelled("Cancelled by user")

    def _record(self, profile: ProviderProfile, capability: str, outcome: str, started: float, *,
                error: AIError | None = None, usage: Usage | None = None) -> None:
        if self.usage_log is None:
            return
        try:
            self.usage_log.record(profile, capability, outcome, int((time.monotonic() - started) * 1000),
                                  error_kind=error.kind if error else None, status=error.status if error else None,
                                  usage=usage)
        except Exception:  # noqa: BLE001 - logging usage must never break a request
            log.exception("Could not record AI usage")

    def request_json(self, capability: str, system: str, user: str) -> AIResult:
        candidates, skipped = self.plan(capability)
        label = CAPABILITIES.get(capability, capability)
        if not candidates:
            reasons = "; ".join(skipped) or "no providers are configured"
            raise AIError(f"No AI provider is available for {label.lower()} ({reasons}). "
                          "Configure one on the AI Models page.", "not_configured")
        notes: list[str] = []
        failures: list[str] = []
        last: AIError | None = None
        for index, (profile, key) in enumerate(candidates):
            if index:
                notes.append(f"Used fallback provider {profile.display_name}.")
            try:
                client = ProviderClient(profile, key, self.transport)
            except AIError as exc:
                failures.append(f"{profile.display_name}: {exc}")
                last = exc
                continue
            attempt = 0
            while True:
                self._check()
                wait = self.limiter.wait_time(profile)
                if wait > self.max_wait:
                    last = AIError(f"{profile.display_name}: local rate limit reached "
                                   f"({profile.rate_limit_per_minute}/min)", "rate_limit")
                    failures.append(str(last))
                    break
                self._wait(wait)
                started = time.monotonic()
                self.limiter.note(profile)
                try:
                    data, reported = client.chat_json(system, user)
                except AIError as exc:
                    last = exc
                    self._record(profile, capability, "error", started, error=exc)
                    log.warning("AI request failed", extra={"provider": profile.display_name, "model": profile.model,
                                                            "kind": exc.kind, "status": exc.status,
                                                            "attempt": attempt + 1})
                    if exc.kind == "bad_request" and exc.status != 404:
                        raise AIError(f"{exc} The request was not sent to other providers.", exc.kind,
                                      exc.status) from None
                    retry = (exc.kind in RETRYABLE and attempt < profile.max_retries) or \
                            (exc.kind == "invalid_response" and attempt == 0 and profile.max_retries > 0)
                    if not retry:
                        failures.append(f"{profile.display_name}: {exc}")
                        break
                    attempt += 1
                    notes.append(f"{profile.display_name}: {exc.kind.replace('_', ' ')}, retrying.")
                    self._wait(min(30.0, 2.0 ** attempt))
                    continue
                usage = compute_usage(profile, reported, system + user, json.dumps(data, ensure_ascii=False))
                self._record(profile, capability, "success", started, usage=usage)
                log.info("AI request succeeded", extra={"provider": profile.display_name, "model": profile.model,
                                                        "capability": capability, "usage_source": usage.source})
                return AIResult(data, profile.id, profile.display_name, profile.model, usage, notes)
        message = "Every AI provider failed: " + " | ".join(failures)
        raise AIError(message, last.kind if last else "not_configured", last.status if last else None)


class RouterProvider:
    """Adapter with the ``request_json(system, user)`` interface used by caption correction."""

    def __init__(self, router: AIRouter, capability: str):
        self.router = router
        self.capability = capability
        self.results: list[AIResult] = []

    def request_json(self, system: str, user: str) -> dict:
        result = self.router.request_json(self.capability, system, user)
        self.results.append(result)
        return result.data

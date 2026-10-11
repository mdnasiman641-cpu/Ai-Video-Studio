"""AI provider jobs: connection and model tests, and title/description suggestions.

Network requests never run on the GUI thread. Payloads contain provider profiles but never
API keys; keys are looked up when the job runs.
"""

from __future__ import annotations

from typing import Callable

from ai_video_studio.ai.client import ProviderClient
from ai_video_studio.ai.profiles import ProviderProfile
from ai_video_studio.ai.providers import AIError
from ai_video_studio.ai.router import AIRouter, compute_usage
from ai_video_studio.jobs.caption_jobs import profile_key_for
from ai_video_studio.jobs.manager import JobContext
from ai_video_studio.jobs.models import Job

AI_TEST_JOB = "ai_test"
AI_TITLES_JOB = "ai_titles"

MAX_TITLE_INPUT = 12_000  # characters of caption text sent for title suggestions

TITLE_SYSTEM_PROMPT = (
    "You write titles and descriptions for short videos from their spoken text. Use the same language as "
    "the text. Do not invent facts that are not in the text. Reply with JSON only: "
    "{\"title\": \"<at most 80 characters>\", \"description\": \"<2-4 sentences>\", "
    "\"hashtags\": [\"#tag\", ...]} with at most 8 hashtags."
)


def make_ai_test_handler(get_api_key: Callable[[str], str | None], transport=None, usage_log=None):
    def handle(job: Job, ctx: JobContext) -> dict:
        p = job.payload
        profile = ProviderProfile.from_dict(p["profile"])
        profile.validate()
        client = ProviderClient(profile, profile_key_for(profile, get_api_key), transport)
        ctx.check_cancelled()
        if p.get("mode") == "model":
            import time

            started = time.monotonic()
            system, user = "You are a connectivity test. Reply with JSON only.", 'Reply with {"ok": true}'
            try:
                data, reported = client.chat_json(system, user)
            except AIError as exc:
                if usage_log is not None:
                    usage_log.record(profile, "test", "error", int((time.monotonic() - started) * 1000),
                                     error_kind=exc.kind, status=exc.status)
                raise
            usage = compute_usage(profile, reported, system + user, str(data))
            if usage_log is not None:
                usage_log.record(profile, "test", "success", int((time.monotonic() - started) * 1000), usage=usage)
            if data.get("ok") is not True:
                raise AIError(f"{profile.model} answered, but not with the requested JSON.", "invalid_response")
            tokens = (f"{usage.input_tokens}+{usage.output_tokens} tokens "
                      f"({'reported by the provider' if usage.source == 'provider' else 'estimated'})")
            return {"profile_id": profile.id, "ok": True,
                    "message": f"Model {profile.model} answered correctly ({tokens})."}
        models = client.list_models()
        listed = profile.model in models
        message = f"Connected: the key works and {len(models)} model(s) are available."
        if models and not listed:
            message += f" '{profile.model}' is not in the provider's list; check the model name."
        return {"profile_id": profile.id, "ok": True, "message": message, "model_listed": listed}

    return handle


def parse_titles(data: dict) -> dict:
    title, description, tags = data.get("title"), data.get("description"), data.get("hashtags", [])
    if not isinstance(title, str) or not title.strip() or not isinstance(description, str):
        raise AIError("The AI answer did not contain a title and description.", "invalid_response")
    if not isinstance(tags, list):
        tags = []
    clean_tags = []
    for tag in tags[:8]:
        if isinstance(tag, str) and tag.strip():
            tag = tag.strip().replace(" ", "")
            clean_tags.append(tag if tag.startswith("#") else "#" + tag)
    return {"title": title.strip()[:120], "description": description.strip()[:2000], "hashtags": clean_tags}


def make_ai_titles_handler(get_api_key: Callable[[str], str | None], transport=None, sleep=None, usage_log=None):
    def handle(job: Job, ctx: JobContext) -> dict:
        import json

        p = job.payload
        profiles = [ProviderProfile.from_dict(d) for d in p["profiles"]]
        router = AIRouter(profiles, lambda prof: profile_key_for(prof, get_api_key), transport=transport, sleep=sleep,
                          usage_log=usage_log, cancel_event=ctx.cancel_event)
        user = json.dumps({"language": p.get("language") or "unknown", "text": p["text"][:MAX_TITLE_INPUT]},
                          ensure_ascii=False)
        result = router.request_json("titles", TITLE_SYSTEM_PROMPT, user)
        return {"media_id": p.get("media_id"), "provider": result.profile_name, "model": result.model,
                "notes": result.notes, **parse_titles(result.data)}

    return handle

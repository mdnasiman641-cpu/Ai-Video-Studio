"""Optional AI caption correction: prompt building, response validation and diffing.

Suggestions are never applied automatically: the UI shows each one next to the original
and only accepted suggestions change the captions.
"""

from __future__ import annotations

import difflib
import json

from ai_video_studio.ai.providers import AIError, JsonProvider
from ai_video_studio.captions.model import CaptionSegment, clean_text

BATCH_SIZE = 60

SYSTEM_PROMPT = (
    "You proofread automatically generated video captions. Fix only clear speech-recognition "
    "mistakes, spelling, capitalisation and punctuation. Keep the original language and wording; "
    "never translate, summarise, censor, add content, or merge/split captions. "
    "Reply with JSON only, in the form {\"corrections\": [{\"id\": \"<id>\", \"text\": \"<corrected text>\"}]}, "
    "listing only captions you changed. If nothing needs changing, reply {\"corrections\": []}."
)


def build_user_prompt(segments: list[dict], language: str | None) -> str:
    payload = {"language": language or "unknown", "captions": [{"id": s["id"], "text": s["text"]} for s in segments]}
    return json.dumps(payload, ensure_ascii=False)


def parse_corrections(response: dict, segments: list[dict]) -> list[dict]:
    """Validate the model's answer. Unknown ids, empty, unchanged or implausibly long texts are ignored."""
    originals = {s["id"]: s["text"] for s in segments}
    items = response.get("corrections")
    if not isinstance(items, list):
        raise AIError("The AI answer did not contain a corrections list.", "invalid_response")
    suggestions = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        seg_id, text = str(item.get("id", "")), item.get("text")
        if seg_id not in originals or seg_id in seen or not isinstance(text, str):
            continue
        suggested = clean_text(text)
        original = originals[seg_id]
        if not suggested or suggested == original or len(suggested) > 3 * len(original) + 20:
            continue
        seen.add(seg_id)
        suggestions.append({"id": seg_id, "original": original, "suggested": suggested})
    return suggestions


def request_corrections(provider: JsonProvider, segments: list[dict], language: str | None,
                        progress=None, check_cancelled=None) -> list[dict]:
    suggestions: list[dict] = []
    batches = [segments[i:i + BATCH_SIZE] for i in range(0, len(segments), BATCH_SIZE)] or [[]]
    for index, batch in enumerate(batches):
        if check_cancelled:
            check_cancelled()
        if not batch:
            continue
        response = provider.request_json(SYSTEM_PROMPT, build_user_prompt(batch, language))
        suggestions.extend(parse_corrections(response, batch))
        if progress:
            progress((index + 1) / len(batches))
    return suggestions


def word_diff(original: str, suggested: str) -> list[tuple[str, str]]:
    """Word-level diff as ``[(op, text)]`` with op in ``equal``, ``delete``, ``insert``."""
    a, b = original.split(), suggested.split()
    out: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            out.append(("equal", " ".join(a[i1:i2])))
        else:
            if i2 > i1:
                out.append(("delete", " ".join(a[i1:i2])))
            if j2 > j1:
                out.append(("insert", " ".join(b[j1:j2])))
    return out


def apply_suggestions(segments: list[CaptionSegment], suggestions: list[dict], accepted_ids: set[str]) -> tuple[int, int]:
    """Apply accepted suggestions whose caption text is still unchanged since the request.

    Returns ``(applied, stale)``; stale suggestions are skipped because the user edited
    that caption after the request was made.
    """
    by_id = {s.id: s for s in segments}
    applied = stale = 0
    for sug in suggestions:
        if sug["id"] not in accepted_ids:
            continue
        seg = by_id.get(sug["id"])
        if seg is None or seg.text != sug["original"]:
            stale += 1
            continue
        seg.text = sug["suggested"]
        seg.words = []
        applied += 1
    return applied, stale

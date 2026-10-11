"""Local log of AI requests: who answered, how long it took and how many tokens were used.

The log never contains prompts, captions or answers. Token counts are marked as reported by the
provider or estimated locally; costs are only shown when the user entered prices for a profile,
and are always labelled as estimates (this is not billing information).
"""

from __future__ import annotations

from ai_video_studio.core.media import now_iso
from ai_video_studio.storage.database import Database

KEEP_ROWS = 2000


class UsageLog:
    def __init__(self, db: Database):
        self._db = db

    def record(self, profile, capability: str, outcome: str, duration_ms: int, *, error_kind: str | None = None,
               status: int | None = None, usage=None) -> None:
        self._db.execute(
            "INSERT INTO ai_requests(created_at, provider_id, provider_name, model, capability, outcome, error_kind, "
            "status_code, duration_ms, input_tokens, output_tokens, usage_source, cost, cost_source) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), profile.id, profile.display_name, profile.model, capability, outcome, error_kind, status,
             duration_ms, usage.input_tokens if usage else None, usage.output_tokens if usage else None,
             usage.source if usage else "none", usage.cost if usage else None,
             usage.cost_source if usage else "unknown"))
        self._db.execute("DELETE FROM ai_requests WHERE id <= (SELECT MAX(id) FROM ai_requests) - ?", (KEEP_ROWS,))

    def recent(self, limit: int = 200) -> list[dict]:
        rows = self._db.query("SELECT * FROM ai_requests ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def totals(self) -> list[dict]:
        rows = self._db.query(
            "SELECT provider_name, model, COUNT(*) AS requests, SUM(outcome = 'error') AS failures, "
            "SUM(CASE WHEN usage_source = 'provider' THEN COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) "
            "ELSE 0 END) AS reported_tokens, "
            "SUM(CASE WHEN usage_source = 'estimated' THEN COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) "
            "ELSE 0 END) AS estimated_tokens, "
            "SUM(cost) AS cost, SUM(outcome = 'success' AND cost IS NULL) AS unknown_cost "
            "FROM ai_requests GROUP BY provider_id, model ORDER BY requests DESC")
        return [dict(r) for r in rows]

    def clear(self) -> None:
        self._db.execute("DELETE FROM ai_requests")

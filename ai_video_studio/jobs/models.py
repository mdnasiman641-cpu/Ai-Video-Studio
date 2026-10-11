"""Job states and the allowed transitions between them."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum


class JobState(str, Enum):
    QUEUED = "queued"
    HELD = "held"            # queued but paused (e.g. a paused batch); not started until released
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_finished(self) -> bool:
        return self in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED)


ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCELLED, JobState.HELD}),
    JobState.HELD: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.RUNNING: frozenset({JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}),
    JobState.FAILED: frozenset({JobState.QUEUED}),      # retry
    JobState.CANCELLED: frozenset({JobState.QUEUED}),   # retry
    JobState.COMPLETED: frozenset(),
}


def can_transition(current: JobState, target: JobState) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


@dataclass
class Job:
    id: str
    kind: str
    title: str
    state: JobState
    progress: float = 0.0
    payload: dict = field(default_factory=dict)
    result: dict | None = None
    error: str | None = None
    attempts: int = 0
    project_id: str | None = None
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    stage: str | None = None   # what a running job is doing (e.g. "analyzing", "rendering")

    @classmethod
    def from_row(cls, row) -> "Job":
        return cls(
            id=row["id"],
            kind=row["kind"],
            title=row["title"],
            state=JobState(row["state"]),
            progress=row["progress"],
            payload=json.loads(row["payload"] or "{}"),
            result=json.loads(row["result"]) if row["result"] else None,
            error=row["error"],
            attempts=row["attempts"],
            project_id=row["project_id"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            stage=row["stage"] if "stage" in row.keys() else None,
        )

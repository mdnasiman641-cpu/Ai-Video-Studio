"""Persistent job storage with validated state transitions."""

from __future__ import annotations

import json
import uuid

from ai_video_studio.core.errors import InvalidStateTransition
from ai_video_studio.core.media import now_iso
from ai_video_studio.jobs.models import Job, JobState, can_transition
from ai_video_studio.storage.database import Database

INTERRUPTED_MESSAGE = "Interrupted: the application closed while this job was running."


class JobStore:
    def __init__(self, db: Database):
        self._db = db

    def create(self, kind: str, title: str, payload: dict | None = None, project_id: str | None = None) -> Job:
        job_id = uuid.uuid4().hex
        self._db.execute(
            "INSERT INTO jobs(id, kind, title, state, payload, project_id, created_at) VALUES(?,?,?,?,?,?,?)",
            (job_id, kind, title, JobState.QUEUED.value, json.dumps(payload or {}), project_id, now_iso()),
        )
        return self.get(job_id)

    def get(self, job_id: str) -> Job:
        row = self._db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if row is None:
            raise KeyError(job_id)
        return Job.from_row(row)

    def list(self, states: set[JobState] | None = None, limit: int = 1000, newest_first: bool = False) -> list[Job]:
        order = "DESC" if newest_first else "ASC"
        where, params = "", ()
        if states:
            where = f"WHERE state IN ({','.join('?' for _ in states)})"
            params = tuple(s.value for s in states)
        rows = self._db.query(
            f"SELECT * FROM jobs {where} ORDER BY created_at {order}, rowid {order} LIMIT ?",
            (*params, limit),
        )
        return [Job.from_row(r) for r in rows]

    def counts(self) -> dict[JobState, int]:
        result = {state: 0 for state in JobState}
        for row in self._db.query("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state"):
            result[JobState(row["state"])] = row["n"]
        return result

    def next_queued(self, limit: int) -> list[Job]:
        rows = self._db.query(
            "SELECT * FROM jobs WHERE state = ? ORDER BY created_at, rowid LIMIT ?",
            (JobState.QUEUED.value, limit),
        )
        return [Job.from_row(r) for r in rows]

    def transition(self, job_id: str, target: JobState, **fields) -> Job:
        """Move a job to ``target`` atomically, rejecting illegal transitions."""
        with self._db.transaction() as conn:
            row = conn.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            current = JobState(row["state"])
            if not can_transition(current, target):
                raise InvalidStateTransition(f"Job cannot go from {current.value} to {target.value}")
            updates = {"state": target.value}
            if target is JobState.RUNNING:
                updates |= {"started_at": now_iso(), "finished_at": None, "progress": 0.0}
            elif target.is_finished:
                updates["finished_at"] = now_iso()
                if target is JobState.COMPLETED:
                    updates["progress"] = 1.0
            elif target is JobState.QUEUED:
                updates |= {"error": None, "result": None, "progress": 0.0, "started_at": None, "finished_at": None}
            for key, value in fields.items():
                updates[key] = json.dumps(value) if key == "result" and value is not None else value
            assignments = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*updates.values(), job_id))
            if target is JobState.RUNNING:
                conn.execute("UPDATE jobs SET attempts = attempts + 1 WHERE id = ?", (job_id,))
        return self.get(job_id)

    def set_progress(self, job_id: str, progress: float) -> None:
        progress = min(max(float(progress), 0.0), 1.0)
        self._db.execute(
            "UPDATE jobs SET progress = ? WHERE id = ? AND state = ?",
            (progress, job_id, JobState.RUNNING.value),
        )

    def recover_interrupted(self) -> list[Job]:
        """Mark jobs left RUNNING by a crash or forced exit as FAILED so they can be retried."""
        rows = self._db.query("SELECT id FROM jobs WHERE state = ?", (JobState.RUNNING.value,))
        return [self.transition(r["id"], JobState.FAILED, error=INTERRUPTED_MESSAGE) for r in rows]

    def delete_finished(self) -> int:
        cur = self._db.execute(
            "DELETE FROM jobs WHERE state IN (?, ?, ?)",
            (JobState.COMPLETED.value, JobState.FAILED.value, JobState.CANCELLED.value),
        )
        return cur.rowcount

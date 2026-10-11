"""SQLite application database (settings, recent projects, job history).

Projects themselves are portable JSON files; the database holds per-user state.
The connection is shared across threads and guarded by a lock.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

# Each entry upgrades the schema by one version. Never edit an entry once released.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE recent_projects (
        path TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        last_opened TEXT NOT NULL
    );
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        state TEXT NOT NULL,
        progress REAL NOT NULL DEFAULT 0,
        payload TEXT NOT NULL DEFAULT '{}',
        result TEXT,
        error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        project_id TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT
    );
    CREATE INDEX idx_jobs_state ON jobs(state);
    CREATE INDEX idx_jobs_created ON jobs(created_at);
    """,
    # 2: user caption style presets (built-in presets live in code)
    """
    CREATE TABLE caption_presets (
        name TEXT PRIMARY KEY,
        style TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    # 3: render progress stage, export presets (Bulk Processing) and saved batches
    """
    ALTER TABLE jobs ADD COLUMN stage TEXT;
    CREATE TABLE render_presets (
        name TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE batches (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        data TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    # 4: AI provider profiles (no secrets: keys stay in the credential store) and a request log
    #    without prompt or response content
    """
    CREATE TABLE ai_providers (
        id TEXT PRIMARY KEY,
        position INTEGER NOT NULL,
        data TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE ai_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        provider_name TEXT NOT NULL,
        model TEXT NOT NULL,
        capability TEXT NOT NULL,
        outcome TEXT NOT NULL,
        error_kind TEXT,
        status_code INTEGER,
        duration_ms INTEGER NOT NULL,
        input_tokens INTEGER,
        output_tokens INTEGER,
        usage_source TEXT NOT NULL,
        cost REAL,
        cost_source TEXT NOT NULL
    );
    CREATE INDEX idx_ai_requests_created ON ai_requests(created_at);
    """,
]


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for index in range(version, len(MIGRATIONS)):
                log.info("Applying database migration", extra={"version": index + 1})
                self._conn.executescript("BEGIN;" + MIGRATIONS[index] + f"; PRAGMA user_version = {index + 1}; COMMIT;")

    @property
    def schema_version(self) -> int:
        with self._lock:
            return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

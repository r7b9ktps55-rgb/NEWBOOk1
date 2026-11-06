"""Очередь задач в SQLite: переживает перезапуски и хранит историю."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('pdf', 'url')),
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

STATUSES = ("queued", "processing", "done", "error")


@dataclass
class Job:
    id: int
    kind: str
    source: str
    title: str
    status: str
    error: str | None
    note: str | None
    created_at: str
    updated_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute(SCHEMA)
            # Если приложение упало посреди обработки — вернуть задачу в очередь.
            c.execute("UPDATE jobs SET status='queued', updated_at=? WHERE status='processing'", (_now(),))

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _run(self, sql: str, args: tuple = ()) -> list[Job]:
        with self._lock, self._conn() as c:
            return [Job(**dict(r)) for r in c.execute(sql, args).fetchall()]

    def add(self, kind: str, source: str, title: str) -> Job:
        now = _now()
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO jobs (kind, source, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                (kind, source, title, now, now),
            )
            job_id = cur.lastrowid
        return self.get(job_id)  # type: ignore[return-value]

    def get(self, job_id: int) -> Job | None:
        rows = self._run("SELECT * FROM jobs WHERE id=?", (job_id,))
        return rows[0] if rows else None

    def has_source(self, kind: str, source: str) -> bool:
        return bool(self._run("SELECT * FROM jobs WHERE kind=? AND source=? LIMIT 1", (kind, source)))

    def list(self, *statuses: str) -> list[Job]:
        if statuses:
            marks = ",".join("?" * len(statuses))
            return self._run(f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY id", statuses)
        return self._run("SELECT * FROM jobs ORDER BY id")

    def claim_next(self) -> Job | None:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return None
            c.execute("UPDATE jobs SET status='processing', error=NULL, updated_at=? WHERE id=?", (_now(), row["id"]))
        return self.get(row["id"])

    def mark_done(self, job_id: int, note: str, title: str | None = None) -> None:
        self._run(
            "UPDATE jobs SET status='done', note=?, title=COALESCE(?, title), error=NULL, updated_at=? WHERE id=?",
            (note, title, _now(), job_id),
        )

    def mark_error(self, job_id: int, message: str) -> None:
        self._run("UPDATE jobs SET status='error', error=?, updated_at=? WHERE id=?", (message[:1000], _now(), job_id))

    def requeue(self, job_id: int) -> bool:
        with self._lock, self._conn() as c:
            cur = c.execute(
                "UPDATE jobs SET status='queued', error=NULL, updated_at=? WHERE id=? AND status='error'",
                (_now(), job_id),
            )
            return cur.rowcount > 0

    def delete(self, job_id: int) -> Job | None:
        """Удаляет задачу, если она не обрабатывается прямо сейчас."""
        job = self.get(job_id)
        if not job or job.status == "processing":
            return None
        self._run("DELETE FROM jobs WHERE id=?", (job_id,))
        return job

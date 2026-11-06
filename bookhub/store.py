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
    kind TEXT NOT NULL CHECK (kind IN ('file', 'url', 'pdf')),
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

# Колонки, добавленные позже: старые базы дополняются при запуске.
MIGRATIONS = {
    "formats": "TEXT NOT NULL DEFAULT 'summary'",
    "done_formats": "TEXT NOT NULL DEFAULT ''",
    "progress": "TEXT",
    "book": "TEXT",
}

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
    formats: str = "summary"
    done_formats: str = ""
    progress: str | None = None
    book: str | None = None

    @property
    def is_file(self) -> bool:
        return self.kind != "url"  # 'pdf' — старое название для файлов

    @property
    def type_label(self) -> str:
        from .extract import label

        return "URL" if self.kind == "url" else label(self.source)

    @property
    def format_list(self) -> list[str]:
        return [f for f in self.formats.split(",") if f]

    @property
    def done_list(self) -> list[str]:
        return [f for f in self.done_formats.split(",") if f]

    @property
    def pending_formats(self) -> list[str]:
        return [f for f in self.format_list if f not in self.done_list]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute(SCHEMA)
            self._add_columns(c)
            # Старые базы разрешали только 'pdf' и 'url' — пересоздаём таблицу с новым ограничением.
            sql = c.execute("SELECT sql FROM sqlite_master WHERE name='jobs'").fetchone()[0]
            if "'file'" not in sql:
                c.execute("ALTER TABLE jobs RENAME TO jobs_old")
                c.execute(SCHEMA)
                self._add_columns(c)
                cols = ", ".join(r["name"] for r in c.execute("PRAGMA table_info(jobs_old)"))
                c.execute(f"INSERT INTO jobs ({cols}) SELECT {cols} FROM jobs_old")
                c.execute("DROP TABLE jobs_old")
            # Если приложение упало посреди обработки — вернуть задачу в очередь.
            c.execute("UPDATE jobs SET status='queued', updated_at=? WHERE status='processing'", (_now(),))

    @staticmethod
    def _add_columns(c: sqlite3.Connection) -> None:
        have = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
        for col, decl in MIGRATIONS.items():
            if col not in have:
                c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _run(self, sql: str, args: tuple = ()) -> list[Job]:
        with self._lock, self._conn() as c:
            return [Job(**dict(r)) for r in c.execute(sql, args).fetchall()]

    def add(self, kind: str, source: str, title: str, formats: list[str] | None = None,
            book: str | None = None) -> Job:
        now = _now()
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO jobs (kind, source, title, formats, book, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (kind, source, title, ",".join(formats or ["summary"]), book, now, now),
            )
            job_id = cur.lastrowid
        return self.get(job_id)  # type: ignore[return-value]

    def get(self, job_id: int) -> Job | None:
        rows = self._run("SELECT * FROM jobs WHERE id=?", (job_id,))
        return rows[0] if rows else None

    def has_file(self, name: str) -> bool:
        return bool(self._run("SELECT * FROM jobs WHERE kind!='url' AND source=? LIMIT 1", (name,)))

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
            c.execute("UPDATE jobs SET status='processing', error=NULL, progress=NULL, updated_at=? WHERE id=?",
                      (_now(), row["id"]))
        return self.get(row["id"])

    def set_progress(self, job_id: int, text: str) -> None:
        self._run("UPDATE jobs SET progress=?, updated_at=? WHERE id=?", (text, _now(), job_id))

    def set_source(self, job_id: int, source: str) -> None:
        self._run("UPDATE jobs SET source=?, updated_at=? WHERE id=?", (source, _now(), job_id))

    def format_done(self, job_id: int, fmt: str, book: str, note: str) -> None:
        job = self.get(job_id)
        done = ",".join([*job.done_list, fmt]) if job else fmt
        self._run("UPDATE jobs SET done_formats=?, book=?, title=?, note=?, updated_at=? WHERE id=?",
                  (done, book, book, note, _now(), job_id))

    def mark_done(self, job_id: int) -> None:
        self._run("UPDATE jobs SET status='done', error=NULL, progress=NULL, updated_at=? WHERE id=?", (_now(), job_id))

    def mark_error(self, job_id: int, message: str) -> None:
        self._run("UPDATE jobs SET status='error', error=?, progress=NULL, updated_at=? WHERE id=?",
                  (message[:1000], _now(), job_id))

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

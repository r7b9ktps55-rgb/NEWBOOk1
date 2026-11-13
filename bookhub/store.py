"""Очередь задач в SQLite: переживает перезапуски и хранит историю.

Схема версионируется через PRAGMA user_version. Перед миграцией делается копия базы
(bookhub.db.bak-v<старая версия>), а сама миграция идёт одной транзакцией:
если процесс упадёт посередине, база останется в прежнем виде.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 3

JOBS_TABLE = """
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
COLUMNS = {
    "formats": "TEXT NOT NULL DEFAULT 'summary'",
    "done_formats": "TEXT NOT NULL DEFAULT ''",
    "progress": "TEXT",
    "book": "TEXT",
}

EXTRA_SCHEMA = """
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_source ON jobs(source);
CREATE TABLE IF NOT EXISTS usage (day TEXT PRIMARY KEY, requests INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
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
        self._migrate()
        with self._conn() as c:
            # Если приложение упало посреди обработки — вернуть задачу в очередь.
            c.execute("UPDATE jobs SET status='queued', updated_at=? WHERE status='processing'", (_now(),))

    # ---------- соединение и миграции ----------
    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:  # commit / rollback
                yield conn
        finally:
            conn.close()

    def _migrate(self) -> None:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)  # транзакциями управляем сами
        conn.row_factory = sqlite3.Row
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version >= SCHEMA_VERSION:
                return
            has_jobs = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()
            if has_jobs and conn.execute("SELECT 1 FROM jobs LIMIT 1").fetchone():
                backup = self.path.with_name(f"{self.path.name}.bak-v{version}")
                with sqlite3.connect(backup) as dst:
                    conn.backup(dst)
                log.info("Перед обновлением схемы сохранена копия базы: %s", backup.name)
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._upgrade(conn)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    @staticmethod
    def _add_columns(c: sqlite3.Connection) -> None:
        have = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
        for col, decl in COLUMNS.items():
            if col not in have:
                c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")

    def _upgrade(self, c: sqlite3.Connection) -> None:
        c.execute(JOBS_TABLE)
        self._add_columns(c)
        # Старые базы разрешали только 'pdf' и 'url' — пересоздаём таблицу с новым ограничением.
        sql = c.execute("SELECT sql FROM sqlite_master WHERE name='jobs'").fetchone()[0]
        if "'file'" not in sql:
            seq = c.execute("SELECT seq FROM sqlite_sequence WHERE name='jobs'").fetchone()
            c.execute("ALTER TABLE jobs RENAME TO jobs_old")
            c.execute(JOBS_TABLE)
            self._add_columns(c)
            cols = ", ".join(r["name"] for r in c.execute("PRAGMA table_info(jobs_old)"))
            c.execute(f"INSERT INTO jobs ({cols}) SELECT {cols} FROM jobs_old")
            c.execute("DROP TABLE jobs_old")
            if seq:  # чтобы номера удалённых задач не выдавались повторно
                c.execute("UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name='jobs'", (seq[0],))
        for stmt in filter(str.strip, EXTRA_SCHEMA.split(";")):
            c.execute(stmt)

    # ---------- задачи ----------
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

    def active_file(self, name: str) -> Job | None:
        """Незавершённая задача, которой принадлежит файл с таким именем."""
        rows = self._run("SELECT * FROM jobs WHERE kind!='url' AND source=? AND status!='done' LIMIT 1", (name,))
        return rows[0] if rows else None

    def list(self, *statuses: str) -> list[Job]:
        if statuses:
            marks = ",".join("?" * len(statuses))
            return self._run(f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY id", statuses)
        return self._run("SELECT * FROM jobs ORDER BY id")

    def claim_next(self, exclude: set[int] | frozenset = frozenset()) -> Job | None:
        """Берёт следующую задачу одним запросом — две копии приложения не возьмут одну и ту же.

        exclude — задачи, отложенные в этом проходе (например, им не хватило сегодняшнего лимита)."""
        skip = ",".join(str(int(i)) for i in exclude) or "0"
        rows = self._run(
            "UPDATE jobs SET status='processing', error=NULL, progress=NULL, updated_at=? "
            f"WHERE id = (SELECT id FROM jobs WHERE status='queued' AND id NOT IN ({skip}) ORDER BY id LIMIT 1) "
            "AND status='queued' RETURNING *",
            (_now(),),
        )
        return rows[0] if rows else None

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

    def requeue_now(self, job_id: int) -> None:
        """Вернуть задачу из обработки в очередь (например, когда кончился дневной лимит)."""
        self._run("UPDATE jobs SET status='queued', progress=NULL, updated_at=? WHERE id=?", (_now(), job_id))

    def delete(self, job_id: int) -> Job | None:
        """Удаляет задачу, если она не обрабатывается прямо сейчас."""
        job = self.get(job_id)
        if not job or job.status == "processing":
            return None
        self._run("DELETE FROM jobs WHERE id=?", (job_id,))
        return job

    # ---------- расход запросов к Gemini ----------
    def take_request(self, day: str, limit: int) -> bool:
        """Засчитывает один запрос за день. False — дневной лимит исчерпан (0 — без лимита)."""
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO usage (day, requests) VALUES (?, 0) ON CONFLICT(day) DO NOTHING", (day,))
            row = c.execute(
                "UPDATE usage SET requests = requests + 1 WHERE day=? AND (? = 0 OR requests < ?) RETURNING requests",
                (day, limit, limit),
            ).fetchone()
            return row is not None

    def requests_on(self, day: str) -> int:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT requests FROM usage WHERE day=?", (day,)).fetchone()
            return row[0] if row else 0

    def prune_usage(self, before_day: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM usage WHERE day < ?", (before_day,))

    # ---------- служебные значения (переживают перезапуск) ----------
    def get_meta(self, key: str) -> str | None:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        with self._lock, self._conn() as c:
            if value is None:
                c.execute("DELETE FROM meta WHERE key=?", (key,))
            else:
                c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, value))

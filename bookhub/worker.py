"""Обработка очереди: берёт задачу, делает заметки нужных форматов, сохраняет их.

Здесь же ежедневный разбор и обслуживание (резервная копия, корзина, кэш)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import Settings
from .extract import djvu_to_pdf, split_name
from .formats import FORMATS
from .gitsync import GitSync
from .notify import Notifier
from .store import Job, JobStore
from .summarizer import MAX_NAME_BYTES, DailyLimitReached, Summarizer, Summary, SummaryError, safe_filename, with_frontmatter
from .trash import Trash

log = logging.getLogger(__name__)

CACHE_DAYS = 30


def unique_path(folder: Path, stem: str, suffix: str) -> Path:
    path = folder / f"{stem}{suffix}"
    n = 2
    while path.exists():
        path = folder / f"{stem} ({n}){suffix}"
        n += 1
    return path


def fit_bytes(text: str, max_bytes: int) -> str:
    while len(text.encode()) > max_bytes:
        text = text[:-1]
    return text.rstrip(" .")


def note_stem(book: str, fmt: str) -> str:
    """Имя заметки: «Книга - Автор (формат)». Если имя длинное, укорачивается книга, а не формат."""
    suffix = f" ({FORMATS[fmt].suffix})" if FORMATS[fmt].suffix else ""
    base = fit_bytes(safe_filename(book, limit=140), MAX_NAME_BYTES - len(suffix.encode()))
    return (base or "Без названия") + suffix


def write_atomic(path: Path, text: str) -> None:
    """Сбой во время записи не оставит обрезанную заметку."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, path)


class Processor:
    def __init__(self, settings: Settings, store: JobStore, summarizer: Summarizer | None, git: GitSync,
                 notifier: Notifier | None = None, trash: Trash | None = None):
        self.s = settings
        self.store = store
        self.summarizer = summarizer
        self.git = git
        self.notifier = notifier or Notifier()
        self.trash = trash or Trash(settings)
        self._lock = asyncio.Lock()
        # Общая блокировка для всего, что выбирает имена файлов в inbox (загрузка и подхват папки).
        self.inbox_lock = threading.Lock()
        self.limit_hit = False

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def today(self) -> str:
        return datetime.now(self.s.tz).strftime("%Y-%m-%d")

    def take_request(self) -> None:
        """Счётчик запросов к Gemini: вызывается перед каждым запросом."""
        if not self.store.take_request(self.today(), self.s.daily_request_limit):
            raise DailyLimitReached(
                f"Дневной лимит запросов к Gemini исчерпан ({self.s.daily_request_limit}, DAILY_REQUEST_LIMIT)"
            )

    # ---------- inbox ----------
    def free_inbox_name(self, stem: str, ext: str) -> Path:
        """Имя, которого нет ни в inbox, ни среди книг, уже известных очереди. Вызывать под inbox_lock."""
        dest, n = self.s.inbox / f"{stem}{ext}", 2
        while dest.exists() or self.store.has_file(dest.name):
            dest, n = self.s.inbox / f"{stem} ({n}){ext}", n + 1
        return dest

    def sync_inbox(self) -> int:
        """Книги, положенные в data/inbox вручную, тоже попадают в очередь."""
        added = 0
        with self.inbox_lock:
            for p in sorted(self.s.inbox.iterdir()):
                stem, ext = split_name(p.name)
                if not p.is_file() or not ext or p.name.startswith("."):
                    continue
                if self.store.active_file(p.name):
                    continue  # это файл задачи, которая ещё ждёт очереди
                if self.store.has_file(p.name):
                    # Имя уже было у другой книги — переименовываем, чтобы не перепутать их.
                    new = self.free_inbox_name(stem, ext)
                    p.rename(new)
                    p, stem = new, split_name(new.name)[0]
                self.store.add("file", p.name, stem, list(self.s.default_formats))
                added += 1
        return added

    def find_file(self, name: str) -> Path | None:
        for folder in (self.s.inbox, self.s.archive):
            path = folder / Path(name).name
            if path.is_file():
                return path
        return None

    # ---------- обработка ----------
    async def run(self, limit: int | None = 1) -> int:
        """Обрабатывает до `limit` задач (None — всю очередь). Возвращает число новых заметок."""
        if self._lock.locked():
            return 0
        async with self._lock:
            self.limit_hit = False
            await asyncio.to_thread(self.sync_inbox)
            notes: list[str] = []
            attempts = 0
            while (limit is None or attempts < limit) and not self.limit_hit:
                job = self.store.claim_next()
                if not job:
                    break
                attempts += 1
                notes += await asyncio.to_thread(self._process, job)
            if notes:
                msg = f"Add {notes[0]}" if len(notes) == 1 else f"Add {len(notes)} book notes"
                had_error = self.git.last_error
                if not await asyncio.to_thread(self.git.sync, msg) and self.git.last_error != had_error:
                    self.notifier.send(f"ошибка синхронизации с Git: {self.git.last_error}")
            return len(notes)

    async def daily(self) -> None:
        """Ежедневный разбор по расписанию: результат запоминается и приходит уведомлением."""
        errors_before = len(self.store.list("error"))
        started = datetime.now(self.s.tz)
        try:
            done = await self.run(self.s.daily_batch_size)
            errors = max(0, len(self.store.list("error")) - errors_before)
            result = f"готово заметок: {done}" + (f", ошибок: {errors}" if errors else "")
            if self.limit_hit:
                result += ", дневной лимит запросов исчерпан"
        except Exception as exc:  # расписание не должно молча умирать
            log.exception("Ежедневный разбор упал")
            result = f"сбой: {exc}"
        self.store.set_meta("last_daily_run", json.dumps({"at": started.strftime("%Y-%m-%d %H:%M"), "result": result},
                                                         ensure_ascii=False))
        if result != "готово заметок: 0":
            self.notifier.send(f"ежедневный разбор — {result}")

    def _process(self, job: Job) -> list[str]:
        written: list[str] = []
        try:
            if not self.summarizer:
                raise SummaryError("GEMINI_API_KEY не задан")
            progress = lambda text: self.store.set_progress(job.id, text)  # noqa: E731
            source: Path | str = job.source
            note_source = job.source
            kind = "url"
            if job.is_file:
                kind = "file"
                book_file = self.find_file(job.source)
                if not book_file:
                    raise SummaryError("Файл книги не найден ни в inbox, ни в archive")
                if book_file.parent == self.s.inbox:
                    # Сразу переносим в архив: в inbox лежит только то, что ещё ждёт очереди.
                    dest = unique_path(self.s.archive, *split_name(book_file.name))
                    shutil.move(str(book_file), dest)
                    self.store.set_source(job.id, dest.name)
                    book_file = dest
                note_source = book_file.name  # имя в архиве — по нему потом найдётся книга для «Сделать ещё»
                source = book_file
                if split_name(book_file.name)[1] in (".djvu", ".djv"):
                    progress("конвертирую DjVu в PDF")
                    source = djvu_to_pdf(book_file, self.s.cache)
            for summary in self.summarizer.run(kind, source, job.pending_formats, job.book, progress):
                note = self._write(summary, note_source)
                self.store.format_done(job.id, summary.fmt, summary.book, note.name)
                written.append(note.stem)
            self.store.mark_done(job.id)
            log.info("Готово: %s -> %s", job.source, ", ".join(written))
        except DailyLimitReached as exc:
            # Не ошибка книги: вернём задачу в очередь, разбор продолжится завтра.
            self.limit_hit = True
            self.store.requeue_now(job.id)
            log.warning("%s — задача %s вернулась в очередь", exc, job.id)
            if self.store.get_meta("limit_notified") != self.today():
                self.store.set_meta("limit_notified", self.today())
                self.notifier.send(f"{exc}. Разбор продолжится завтра.")
        except Exception as exc:  # ошибка одной книги не должна ронять очередь
            log.exception("Ошибка обработки задачи %s", job.id)
            done = f" (готово: {len(written)})" if written else ""
            message = (str(exc) or exc.__class__.__name__) + done
            self.store.mark_error(job.id, message)
            self.notifier.send(f"не удалось обработать «{job.book or job.title}»: {message}")
        return written

    def _write(self, summary: Summary, source: str) -> Path:
        meta = {
            "title": summary.title,
            "author": summary.author,
            "book": summary.book,
            "format": summary.fmt,
            "tags": ["book", f"book/{summary.fmt}"],
            "status": "unread",
            "created": datetime.now(self.s.tz).strftime("%Y-%m-%d"),
            "source": source,
            **(summary.extra or {}),
        }
        self.s.books_dir.mkdir(parents=True, exist_ok=True)  # git мог удалить пустую папку
        note = unique_path(self.s.books_dir, note_stem(summary.book, summary.fmt), ".md")
        write_atomic(note, with_frontmatter(summary.markdown, meta))
        return note

    # ---------- обслуживание ----------
    def maintenance(self) -> dict:
        """Раз в сутки: резервная копия, очистка корзины и кэша DjVu, старые счётчики."""
        from .backup import make_backup

        report: dict = {}
        try:
            report["backup"] = make_backup(self.s).name
        except Exception as exc:
            log.exception("Резервная копия не создана")
            report["backup_error"] = str(exc)
            self.notifier.send(f"резервная копия не создана: {exc}")
        report["trash_purged"] = self.trash.purge(self.s.trash_days)
        report["cache_purged"] = self.clean_cache()
        self.store.prune_usage((datetime.now(self.s.tz) - timedelta(days=90)).strftime("%Y-%m-%d"))
        self.store.set_meta("last_maintenance", json.dumps(
            {"at": datetime.now(self.s.tz).strftime("%Y-%m-%d %H:%M"), **report}, ensure_ascii=False))
        return report

    def clean_cache(self) -> int:
        """PDF из DjVu: удаляем, если книги больше нет в архиве или копия старше CACHE_DAYS."""
        if not self.s.cache.is_dir():
            return 0
        removed = 0
        limit = time.time() - CACHE_DAYS * 86400
        for pdf in self.s.cache.glob("*.pdf"):
            source = self.s.archive / pdf.name[: -len(".pdf")]
            if not source.exists() or pdf.stat().st_mtime < limit:
                pdf.unlink(missing_ok=True)
                removed += 1
        return removed

"""Обработка очереди: берёт задачу, делает заметки нужных форматов, сохраняет их."""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Settings
from .extract import djvu_to_pdf, split_name
from .formats import FORMATS
from .gitsync import GitSync
from .store import Job, JobStore
from .summarizer import Summarizer, Summary, SummaryError, safe_filename, with_frontmatter

log = logging.getLogger(__name__)


def unique_path(folder: Path, stem: str, suffix: str) -> Path:
    path = folder / f"{stem}{suffix}"
    n = 2
    while path.exists():
        path = folder / f"{stem} ({n}){suffix}"
        n += 1
    return path


def note_stem(book: str, fmt: str) -> str:
    suffix = FORMATS[fmt].suffix
    return safe_filename(f"{book} ({suffix})" if suffix else book, limit=140)


class Processor:
    def __init__(self, settings: Settings, store: JobStore, summarizer: Summarizer | None, git: GitSync):
        self.s = settings
        self.store = store
        self.summarizer = summarizer
        self.git = git
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def sync_inbox(self) -> int:
        """Книги, положенные в data/inbox вручную, тоже попадают в очередь."""
        added = 0
        for p in sorted(self.s.inbox.iterdir()):
            stem, ext = split_name(p.name)
            if p.is_file() and ext and not self.store.has_file(p.name):
                self.store.add("file", p.name, stem, self.s.default_formats)
                added += 1
        return added

    def find_file(self, name: str) -> Path | None:
        for folder in (self.s.inbox, self.s.archive):
            path = folder / Path(name).name
            if path.is_file():
                return path
        return None

    async def run(self, limit: int | None = 1) -> int:
        """Обрабатывает до `limit` задач (None — всю очередь). Возвращает число новых заметок."""
        if self._lock.locked():
            return 0
        async with self._lock:
            await asyncio.to_thread(self.sync_inbox)
            notes: list[str] = []
            attempts = 0
            while limit is None or attempts < limit:
                job = self.store.claim_next()
                if not job:
                    break
                attempts += 1
                notes += await asyncio.to_thread(self._process, job)
            if notes:
                msg = f"Add {notes[0]}" if len(notes) == 1 else f"Add {len(notes)} book notes"
                await asyncio.to_thread(self.git.sync, msg)
            return len(notes)

    def _process(self, job: Job) -> list[str]:
        written: list[str] = []
        try:
            if not self.summarizer:
                raise SummaryError("GEMINI_API_KEY не задан")
            progress = lambda text: self.store.set_progress(job.id, text)  # noqa: E731
            source: Path | str = job.source
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
                source = book_file
                if split_name(book_file.name)[1] in (".djvu", ".djv"):
                    progress("конвертирую DjVu в PDF")
                    source = djvu_to_pdf(book_file, self.s.data_dir / "cache")
            for summary in self.summarizer.run(kind, source, job.pending_formats, job.book, progress):
                note = self._write(summary, Path(job.source).name if job.is_file else job.source)
                self.store.format_done(job.id, summary.fmt, summary.book, note.name)
                written.append(note.stem)
            self.store.mark_done(job.id)
            log.info("Готово: %s -> %s", job.source, ", ".join(written))
        except Exception as exc:  # ошибка одной книги не должна ронять очередь
            log.exception("Ошибка обработки задачи %s", job.id)
            done = f" (готово: {len(written)})" if written else ""
            self.store.mark_error(job.id, (str(exc) or exc.__class__.__name__) + done)
        return written

    def _write(self, summary: Summary, source: str) -> Path:
        meta = {
            "title": summary.title,
            "author": summary.author,
            "book": summary.book,
            "format": summary.fmt,
            "tags": ["book", f"book/{summary.fmt}"],
            "status": "unread",
            "created": datetime.now(ZoneInfo(self.s.timezone)).strftime("%Y-%m-%d"),
            "source": source,
            **(summary.extra or {}),
        }
        note = unique_path(self.s.books_dir, note_stem(summary.book, summary.fmt), ".md")
        note.write_text(with_frontmatter(summary.markdown, meta), "utf-8")
        return note

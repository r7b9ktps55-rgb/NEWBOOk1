"""Обработка очереди: берёт задачу, делает конспект, сохраняет заметку."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from .config import Settings
from .gitsync import GitSync
from .store import Job, JobStore
from .summarizer import Summarizer, SummaryError, add_source

log = logging.getLogger(__name__)


def unique_path(folder: Path, stem: str, suffix: str) -> Path:
    path = folder / f"{stem}{suffix}"
    n = 2
    while path.exists():
        path = folder / f"{stem} ({n}){suffix}"
        n += 1
    return path


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
        """PDF, положенные в data/inbox вручную, тоже попадают в очередь."""
        added = 0
        for p in sorted(self.s.inbox.iterdir()):
            if p.is_file() and p.suffix.lower() == ".pdf" and not self.store.has_source("pdf", p.name):
                self.store.add("pdf", p.name, p.stem)
                added += 1
        return added

    async def run(self, limit: int | None = 1) -> int:
        """Обрабатывает до `limit` задач (None — всю очередь). Возвращает число готовых."""
        if self._lock.locked():
            return 0
        async with self._lock:
            await asyncio.to_thread(self.sync_inbox)
            done: list[str] = []
            attempts = 0
            while limit is None or attempts < limit:
                job = self.store.claim_next()
                if not job:
                    break
                attempts += 1
                title = await asyncio.to_thread(self._process, job)
                if title:
                    done.append(title)
            if done:
                msg = f"Add {done[0]}" if len(done) == 1 else f"Add {len(done)} book summaries"
                await asyncio.to_thread(self.git.sync, msg)
            return len(done)

    def _process(self, job: Job) -> str | None:
        try:
            if not self.summarizer:
                raise SummaryError("GEMINI_API_KEY не задан")
            if job.kind == "pdf":
                pdf = self.s.inbox / job.source
                if not pdf.is_file():
                    raise SummaryError("Файл PDF не найден в inbox")
                summary = self.summarizer.summarize_pdf(pdf)
            else:
                summary = self.summarizer.summarize_url(job.source)
            note = unique_path(self.s.books_dir, summary.title, ".md")
            note.write_text(add_source(summary.markdown, job.source), "utf-8")
            if job.kind == "pdf":
                shutil.move(str(pdf), unique_path(self.s.archive, pdf.stem, pdf.suffix))
            self.store.mark_done(job.id, note.name, summary.title)
            log.info("Готово: %s -> %s", job.source, note.name)
            return summary.title
        except Exception as exc:  # ошибка одной книги не должна ронять очередь
            log.exception("Ошибка обработки задачи %s", job.id)
            self.store.mark_error(job.id, str(exc) or exc.__class__.__name__)
            return None

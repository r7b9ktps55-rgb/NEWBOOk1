"""Список готовых заметок, сгруппированный по книгам."""

from __future__ import annotations

import json
import os
import threading
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from .formats import FORMATS

FRONTMATTER_LIMIT = 64 * 1024  # дальше frontmatter не ищем


def read_meta(path: Path) -> dict[str, str]:
    """Читает поля `ключ: значение` из frontmatter заметки."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            first = f.readline()
            if first.rstrip("\r\n") != "---":
                return {}
            lines, size = [first], len(first)
            for line in f:
                lines.append(line)
                size += len(line)
                if line.rstrip("\r\n") == "---" or size > FRONTMATTER_LIMIT:
                    break
    except OSError:
        return {}
    return parse_meta("".join(lines))


def parse_meta(head: str) -> dict[str, str]:
    """Поля frontmatter из начала текста заметки."""
    if not head.startswith("---\n"):
        return {}
    end = head.find("\n---", 4)
    meta = {}
    for line in head[4:end if end != -1 else None].splitlines():
        key, sep, value = line.partition(":")
        if not sep or not key.strip().replace("_", "").replace("-", "").isalnum():
            continue
        value = value.strip()
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                value = value.strip('"')
        meta[key.strip()] = value
    return meta


@dataclass
class NoteRef:
    name: str  # имя файла
    fmt: str
    label: str


@dataclass
class Book:
    name: str
    mtime: float = 0
    notes: list[NoteRef] = field(default_factory=list)
    source: str = ""

    @property
    def formats(self) -> set[str]:
        return {n.fmt for n in self.notes}

    @property
    def title(self) -> str:
        return self.name.rsplit(" - ", 1)[0].strip() or self.name

    @property
    def author(self) -> str:
        parts = self.name.rsplit(" - ", 1)
        return parts[1].strip() if len(parts) == 2 else ""

    @property
    def spine(self) -> int:
        """Цвет корешка карточки: стабильный для одной и той же книги."""
        return zlib.crc32(self.name.encode()) % 4


# Кэш frontmatter: (папка, имя файла) -> (mtime_ns, размер, поля). Обновление очереди опрашивает
# библиотеку каждые несколько секунд — без кэша это чтение тысяч файлов каждый раз.
_cache: dict[tuple[str, str], tuple[int, int, dict[str, str]]] = {}
_cache_lock = threading.Lock()


def _meta_cached(folder: Path, entry: os.DirEntry) -> tuple[dict[str, str], float]:
    st = entry.stat()
    key = (str(folder), entry.name)
    with _cache_lock:
        hit = _cache.get(key)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2], st.st_mtime
    meta = read_meta(Path(entry.path))
    with _cache_lock:
        _cache[key] = (st.st_mtime_ns, st.st_size, meta)
    return meta, st.st_mtime


def library(books_dir: Path) -> list[Book]:
    books: dict[tuple[str, str], Book] = {}
    seen: set[tuple[str, str]] = set()
    try:
        entries = [e for e in os.scandir(books_dir) if e.name.endswith(".md") and e.is_file()]
    except FileNotFoundError:
        return []
    for entry in entries:
        seen.add((str(books_dir), entry.name))
        meta, mtime = _meta_cached(books_dir, entry)
        fmt = meta.get("format", "summary")
        if fmt not in FORMATS:
            fmt = "summary"
        name = meta.get("book") or entry.name[:-3]
        # Книги с одинаковым названием, но из разных файлов — разные книги.
        key = (name, meta.get("source", ""))
        book = books.setdefault(key, Book(name, source=key[1]))
        book.notes.append(NoteRef(entry.name, fmt, FORMATS[fmt].label))
        book.mtime = max(book.mtime, mtime)
    with _cache_lock:  # забываем удалённые заметки
        for key in [k for k in _cache if k[0] == str(books_dir) and k not in seen]:
            del _cache[key]
    order = list(FORMATS)
    for book in books.values():
        book.notes.sort(key=lambda n: (order.index(n.fmt), n.name))
    return sorted(books.values(), key=lambda b: b.mtime, reverse=True)


def find_book(books_dir: Path, note_name: str) -> Book | None:
    for book in library(books_dir):
        if any(n.name == note_name for n in book.notes):
            return book
    return None

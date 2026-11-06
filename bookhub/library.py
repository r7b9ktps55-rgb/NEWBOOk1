"""Список готовых заметок, сгруппированный по книгам."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .formats import FORMATS


def read_meta(path: Path) -> dict[str, str]:
    """Читает простые поля `ключ: значение` из frontmatter (первые 4 КБ файла)."""
    try:
        with path.open(encoding="utf-8") as f:
            head = f.read(4096)
    except OSError:
        return {}
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


def library(books_dir: Path) -> list[Book]:
    books: dict[str, Book] = {}
    for path in books_dir.glob("*.md"):
        meta = read_meta(path)
        fmt = meta.get("format", "summary")
        if fmt not in FORMATS:
            fmt = "summary"
        name = meta.get("book") or path.stem
        book = books.setdefault(name, Book(name))
        book.notes.append(NoteRef(path.name, fmt, FORMATS[fmt].label))
        book.mtime = max(book.mtime, path.stat().st_mtime)
        book.source = book.source or meta.get("source", "")
    order = list(FORMATS)
    for book in books.values():
        book.notes.sort(key=lambda n: (order.index(n.fmt), n.name))
    return sorted(books.values(), key=lambda b: b.mtime, reverse=True)


def find_book(books_dir: Path, note_name: str) -> Book | None:
    for book in library(books_dir):
        if any(n.name == note_name for n in book.notes):
            return book
    return None

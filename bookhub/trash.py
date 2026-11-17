"""Корзина: удалённые заметки и книги не пропадают сразу.

Каждый удалённый файл лежит в data/trash/<метка>/ вместе с файлом .origin, где записано,
откуда он. С ним могут лежать связанные файлы (.extra/ и .extra.json — например, файл книги,
удалённый вместе с её последней заметкой) и .job.json — задача очереди, чтобы «Отменить»
вернул её с теми же форматами. Через TRASH_DAYS дней корзина чистится автоматически.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import Settings


@dataclass
class TrashItem:
    id: str
    name: str
    origin: str  # путь относительно data/
    deleted_at: float


class Trash:
    def __init__(self, settings: Settings):
        self.s = settings
        self.root = settings.trash

    def _rel(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.s.data_dir.resolve()))

    def put(self, path: Path, *, also: list[Path] | tuple = (), job: dict | None = None) -> str:
        """Переносит файл (и связанные с ним) в корзину и возвращает её номер для «Отменить»."""
        item_id = time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
        box = self.root / item_id
        box.mkdir(parents=True)
        (box / ".origin").write_text(self._rel(path), "utf-8")
        if job:
            (box / ".job.json").write_text(json.dumps(job, ensure_ascii=False), "utf-8")
        extras = {}
        for i, extra in enumerate(also):
            (box / ".extra").mkdir(exist_ok=True)
            key = f"{i}-{extra.name}"
            extras[key] = self._rel(extra)
            shutil.move(str(extra), box / ".extra" / key)
        if extras:
            (box / ".extra.json").write_text(json.dumps(extras, ensure_ascii=False), "utf-8")
        shutil.move(str(path), box / path.name)
        return item_id

    def job(self, item_id: str) -> dict | None:
        """Задача очереди, удалённая вместе с файлом (форматы и т.п.), если она была."""
        box = self._box(item_id)
        try:
            return json.loads((box / ".job.json").read_text("utf-8")) if box else None
        except (OSError, ValueError):
            return None

    def _box(self, item_id: str) -> Path | None:
        box = (self.root / item_id).resolve()
        if box.parent != self.root.resolve() or not (box / ".origin").is_file():
            return None
        return box

    def items(self) -> list[TrashItem]:
        out = []
        if not self.root.is_dir():
            return out
        for box in sorted(self.root.iterdir(), reverse=True):
            files = [p for p in box.iterdir() if not p.name.startswith(".")] if box.is_dir() else []
            if files and (box / ".origin").is_file():
                out.append(TrashItem(box.name, files[0].name, (box / ".origin").read_text("utf-8"),
                                     box.stat().st_mtime))
        return out

    def restore(self, item_id: str) -> Path | None:
        """Возвращает файл на место (если имя занято — с суффиксом «(2)»)."""
        box = self._box(item_id)
        if not box:
            return None
        files = [p for p in box.iterdir() if not p.name.startswith(".")]
        if not files:
            return None
        dest = self._put_back(files[0], (box / ".origin").read_text("utf-8"))
        if dest is None:
            return None
        try:
            extras = json.loads((box / ".extra.json").read_text("utf-8"))
        except (OSError, ValueError):
            extras = {}
        for key, origin in extras.items():
            src = box / ".extra" / key
            if src.is_file():
                self._put_back(src, origin)
        shutil.rmtree(box, ignore_errors=True)
        return dest

    def _put_back(self, src: Path, rel_origin: str) -> Path | None:
        origin = (self.s.data_dir / rel_origin).resolve()
        if self.s.data_dir.resolve() not in origin.parents:
            return None
        origin.parent.mkdir(parents=True, exist_ok=True)
        dest, n = origin, 2
        while dest.exists():
            dest = origin.with_name(f"{origin.stem} ({n}){origin.suffix}")
            n += 1
        shutil.move(str(src), dest)
        return dest

    def purge(self, older_than_days: int | None = None) -> int:
        """Удаляет навсегда всё старше срока (или всё, если срок не задан)."""
        if not self.root.is_dir():
            return 0
        limit = None if older_than_days is None else time.time() - older_than_days * 86400
        removed = 0
        for box in self.root.iterdir():
            if box.is_dir() and (limit is None or box.stat().st_mtime < limit):
                shutil.rmtree(box, ignore_errors=True)
                removed += 1
        return removed

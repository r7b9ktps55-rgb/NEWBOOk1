"""Резервные копии Book Hub.

Копия — один архив .tar.gz: согласованный снимок базы (через SQLite backup API, безопасно
на работающем сайте), хранилище заметок вместе с .git, свои промпты и, если включено
BACKUP_BOOKS, исходные книги.

Запуск вручную:
    python -m bookhub.backup create            # сделать копию сейчас
    python -m bookhub.backup list              # список копий
    python -m bookhub.backup restore ФАЙЛ      # восстановить в пустую папку данных
    python -m bookhub.backup restore ФАЙЛ --force   # поверх существующих данных (сайт должен быть остановлен)
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
import tarfile
from datetime import datetime
from pathlib import Path

from .config import ConfigError, Settings

log = logging.getLogger(__name__)

NOTES_PARTS = ("obsidian_vault", "prompts", "prompt.md")
BOOK_PARTS = ("archive", "inbox")


def _snapshot_db(db: Path, target: Path) -> None:
    src = sqlite3.connect(db)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def make_backup(s: Settings, include_books: bool | None = None, now: datetime | None = None) -> Path:
    include_books = s.backup_books if include_books is None else include_books
    s.backups.mkdir(parents=True, exist_ok=True)
    s.uploads_tmp.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(s.tz)).strftime("%Y-%m-%d_%H%M%S-%f")
    target = s.backups / f"bookhub-{stamp}.tar.gz"
    tmp = s.backups / f".bookhub-{stamp}.part"
    db_copy = s.uploads_tmp / f"backup-{stamp}.db"
    try:
        if s.db_path.exists():
            _snapshot_db(s.db_path, db_copy)
        with tarfile.open(tmp, "w:gz") as tar:
            if db_copy.exists():
                tar.add(db_copy, arcname="data/bookhub.db")
            for name in NOTES_PARTS + (BOOK_PARTS if include_books else ()):
                path = s.data_dir / name
                if path.exists():
                    tar.add(path, arcname=f"data/{name}")
        tmp.replace(target)
    finally:
        db_copy.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)
    prune(s)
    log.info("Резервная копия: %s", target)
    return target


def list_backups(s: Settings) -> list[Path]:
    if not s.backups.is_dir():
        return []
    return sorted(s.backups.glob("bookhub-*.tar.gz"), reverse=True)


def prune(s: Settings) -> None:
    for old in list_backups(s)[max(1, s.backup_keep):]:
        old.unlink(missing_ok=True)


def _has_data(path: Path) -> bool:
    return path.is_file() or (path.is_dir() and any(path.iterdir()))


def restore(s: Settings, archive: Path, force: bool = False) -> Path | None:
    """Распаковывает копию в папку данных.

    Без --force — только если всё, что есть в копии, на месте пусто. С --force текущие данные не стираются,
    а переносятся в data/.pre-restore-<время>; путь к ним возвращается."""
    with tarfile.open(archive, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name.startswith("data/")]
        names = sorted({Path(m.name).parts[1] for m in members if len(Path(m.name).parts) > 1})
        busy = [n for n in names if _has_data(s.data_dir / n)]
        if busy and not force:
            raise ConfigError(
                f"В {s.data_dir} уже есть данные ({', '.join(busy)}). Остановите сайт и добавьте --force: "
                "текущие данные будут перенесены в папку .pre-restore-…, а не удалены."
            )
        s.data_dir.mkdir(parents=True, exist_ok=True)
        # Распаковываем внутрь папки данных: в контейнере писать можно только туда.
        stage = s.data_dir / ".restore-tmp"
        shutil.rmtree(stage, ignore_errors=True)
        tar.extractall(stage, members=members, filter="data")  # filter="data" не пускает пути за пределы папки
    kept = None
    for item in (stage / "data").iterdir():
        dest = s.data_dir / item.name
        if dest.exists() or dest.is_symlink():
            if kept is None:
                kept = s.data_dir / f".pre-restore-{datetime.now(s.tz).strftime('%Y-%m-%d_%H%M%S')}"
                kept.mkdir()
            shutil.move(str(dest), kept / item.name)
        shutil.move(str(item), dest)
    shutil.rmtree(stage, ignore_errors=True)
    log.info("Данные восстановлены из %s%s", archive.name, f"; прежние сохранены в {kept.name}" if kept else "")
    return kept


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(prog="python -m bookhub.backup", description="Резервные копии Book Hub")
    sub = parser.add_subparsers(dest="cmd", required=True)
    create = sub.add_parser("create", help="сделать копию сейчас")
    create.add_argument("--books", action="store_true", help="включить исходные книги (archive, inbox)")
    sub.add_parser("list", help="показать копии")
    rest = sub.add_parser("restore", help="восстановить из копии")
    rest.add_argument("file", type=Path)
    rest.add_argument("--force", action="store_true", help="заменить существующие данные")
    args = parser.parse_args(argv)
    s = Settings.from_env()
    try:
        if args.cmd == "create":
            print(make_backup(s, include_books=args.books or None))
        elif args.cmd == "list":
            for p in list_backups(s):
                print(f"{p.name}\t{p.stat().st_size / 1024 / 1024:.1f} МБ")
        else:
            kept = restore(s, args.file, force=args.force)
            if kept:
                print(f"Прежние данные сохранены в {kept}")
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

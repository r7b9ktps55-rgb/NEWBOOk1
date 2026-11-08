"""Настройки из переменных окружения (и файла .env)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .formats import parse_formats


class ConfigError(Exception):
    pass


MIN_PASSWORD = 10


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должно быть числом, сейчас: {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    web_password: str
    web_user: str = "me"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    data_dir: Path = Path("data")
    vault_subfolder: str = "Books"
    obsidian_vault_name: str = ""
    # "HH:MM" или пусто — тогда ежедневный запуск выключен
    daily_run_time: str = "09:00"
    daily_batch_size: int = 1
    timezone: str = "Europe/Saratov"
    git_repo_url: str = ""
    git_branch: str = "main"
    git_token: str = ""
    git_author_name: str = "Book Hub"
    git_author_email: str = "bookhub@localhost"
    max_upload_mb: int = 200
    # Какие форматы отмечены по умолчанию (и для PDF, положенных в inbox вручную)
    default_formats: tuple[str, ...] = ("summary",)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        env = lambda name, default="": os.getenv(name, default).strip()  # noqa: E731
        return cls(
            web_password=env("WEB_PASSWORD"),
            web_user=env("WEB_USER", "me") or "me",
            gemini_api_key=env("GEMINI_API_KEY"),
            gemini_model=env("GEMINI_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash",
            data_dir=Path(env("DATA_DIR", "data") or "data"),
            vault_subfolder=env("OBSIDIAN_VAULT_SUBFOLDER", "Books"),
            obsidian_vault_name=env("OBSIDIAN_VAULT_NAME"),
            daily_run_time=env("DAILY_RUN_TIME", "09:00"),
            daily_batch_size=_int("DAILY_BATCH_SIZE", 1),
            timezone=env("TIMEZONE", "Europe/Saratov") or "Europe/Saratov",
            git_repo_url=env("GIT_REPO_URL"),
            git_branch=env("GIT_BRANCH", "main") or "main",
            git_token=env("GIT_TOKEN"),
            git_author_name=env("GIT_AUTHOR_NAME", "Book Hub") or "Book Hub",
            git_author_email=env("GIT_AUTHOR_EMAIL", "bookhub@localhost") or "bookhub@localhost",
            max_upload_mb=_int("MAX_UPLOAD_MB", 200),
            default_formats=tuple(parse_formats(env("DEFAULT_FORMATS", "summary"))),
        )

    def validate(self) -> None:
        if not self.web_password:
            raise ConfigError("Задайте WEB_PASSWORD в .env — без пароля сайт не запустится.")
        if len(self.web_password) < MIN_PASSWORD:
            raise ConfigError(f"WEB_PASSWORD слишком короткий: нужно не меньше {MIN_PASSWORD} символов.")
        if self.daily_time() is None and self.daily_run_time not in ("", "off"):
            raise ConfigError(f"DAILY_RUN_TIME должно быть в формате ЧЧ:ММ, сейчас: {self.daily_run_time!r}")
        if ".." in Path(self.vault_subfolder).parts or Path(self.vault_subfolder).is_absolute():
            raise ConfigError("OBSIDIAN_VAULT_SUBFOLDER должна быть относительной папкой внутри хранилища.")
        if self.daily_batch_size < 1:
            raise ConfigError("DAILY_BATCH_SIZE должно быть ≥ 1.")

    @property
    def max_request_bytes(self) -> int:
        """Лимит одного запроса: до пяти книг максимального размера за раз."""
        return (self.max_upload_mb * 5 + 1) * 1024 * 1024

    def daily_time(self) -> tuple[int, int] | None:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", self.daily_run_time)
        if not m:
            return None
        h, mi = int(m.group(1)), int(m.group(2))
        return (h, mi) if h < 24 and mi < 60 else None

    # Папки внутри data/
    @property
    def inbox(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def archive(self) -> Path:
        return self.data_dir / "archive"

    @property
    def vault(self) -> Path:
        return self.data_dir / "obsidian_vault"

    @property
    def books_dir(self) -> Path:
        return self.vault / self.vault_subfolder if self.vault_subfolder else self.vault

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bookhub.db"

    def ensure_dirs(self) -> None:
        for d in (self.inbox, self.archive, self.books_dir):
            d.mkdir(parents=True, exist_ok=True)

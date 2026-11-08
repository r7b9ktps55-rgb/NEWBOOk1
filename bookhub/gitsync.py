"""Отправка заметок в Git-репозиторий хранилища Obsidian (по желанию)."""

from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Settings

log = logging.getLogger(__name__)

GIT_TIMEOUT_S = 180


class GitError(Exception):
    pass


class GitSync:
    def __init__(self, settings: Settings, store=None):
        self.s = settings
        self.vault = settings.vault
        self._lock = threading.Lock()
        self.store = store  # если задан — состояние переживает перезапуск
        self.last_error: str | None = store.get_meta("git_last_error") if store else None
        self.last_sync: str | None = store.get_meta("git_last_sync") if store else None

    def _remember(self) -> None:
        if self.store:
            self.store.set_meta("git_last_error", self.last_error)
            self.store.set_meta("git_last_sync", self.last_sync)

    @property
    def enabled(self) -> bool:
        return bool(self.s.git_repo_url)

    def _basic(self) -> str:
        return base64.b64encode(f"x-access-token:{self.s.git_token}".encode()).decode()

    def _mask(self, text: str) -> str:
        """Убирает из текста токен в любом виде и логин:пароль из адресов."""
        if self.s.git_token:
            text = text.replace(self._basic(), "***").replace(self.s.git_token, "***")
        return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", text)

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if self.s.git_token:
            # Токен — через переменные окружения процесса git: его нет ни в аргументах (ps), ни в .git/config.
            env.update({
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {self._basic()}",
            })
        try:
            res = subprocess.run(["git", *args], cwd=self.vault, capture_output=True, text=True,
                                 timeout=GIT_TIMEOUT_S, env=env)
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0]}: нет ответа дольше {GIT_TIMEOUT_S} с") from None
        if check and res.returncode != 0:
            raise GitError(self._mask(f"git {args[0]}: {(res.stderr or res.stdout).strip()}"))
        return res

    def _configure(self) -> None:
        """Адрес и автор — из настроек при каждой синхронизации: их могли поправить в .env."""
        self._git("remote", "set-url", "origin", self.s.git_repo_url)
        self._git("config", "user.name", self.s.git_author_name)
        self._git("config", "user.email", self.s.git_author_email)

    def _ensure_repo(self) -> None:
        if (self.vault / ".git").exists():
            self._configure()
            return
        self.vault.mkdir(parents=True, exist_ok=True)
        self._git("init", "-b", self.s.git_branch)
        self._git("remote", "add", "origin", self.s.git_repo_url)
        self._configure()
        fetch = self._git("fetch", "origin", self.s.git_branch, check=False)
        if fetch.returncode == 0:
            # В репозитории уже есть хранилище: встаём поверх него. Для файлов, которые есть и там, и здесь,
            # берём версию из репозитория (её могли править в Obsidian), новые локальные заметки остаются.
            self._git("reset", f"origin/{self.s.git_branch}")
            self._git("checkout", "--", ".", check=False)
        elif "couldn't find remote ref" not in fetch.stderr:
            # Репозиторий недоступен (опечатка в адресе, нет сети, неверный токен): не оставляем .git,
            # не связанный с удалённой историей, — в следующий раз присоединимся заново.
            shutil.rmtree(self.vault / ".git", ignore_errors=True)
            raise GitError(self._mask(f"git fetch: {fetch.stderr.strip()}"))

    def _merge_preferring_remote(self) -> None:
        """Конфликт при pull: слияние, где побеждает версия из репозитория."""
        self._git("rebase", "--abort", check=False)
        merge = self._git("merge", "--no-edit", "--allow-unrelated-histories", "-X", "theirs",
                          f"origin/{self.s.git_branch}", check=False)
        if merge.returncode == 0:
            return
        if not (self.vault / ".git" / "MERGE_HEAD").exists():  # слияние даже не началось — это не конфликт
            raise GitError(self._mask(f"git merge: {(merge.stderr or merge.stdout).strip()}"))
        unmerged = self._git("diff", "--name-only", "--diff-filter=U").stdout.split("\n")
        for path in filter(None, unmerged):
            # Файл есть в репозитории — берём его оттуда; удалён там — удаляем и у нас.
            if self._git("checkout", "--theirs", "--", path, check=False).returncode == 0:
                self._git("add", "--", path)
            else:
                self._git("rm", "-f", "--quiet", "--", path)
        self._git("commit", "--no-edit", "-m", "Merge vault changes")
        log.warning("Git: конфликт решён в пользу версии из репозитория")

    def sync(self, message: str) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            try:
                self._ensure_repo()
                self._git("add", "-A")
                if self._git("diff", "--cached", "--quiet", check=False).returncode != 0:
                    self._git("commit", "-m", message)
                fetch = self._git("fetch", "origin", self.s.git_branch, check=False)
                if fetch.returncode == 0:
                    pull = self._git("rebase", f"origin/{self.s.git_branch}", check=False)
                    if pull.returncode != 0:
                        self._merge_preferring_remote()
                elif "couldn't find remote ref" not in fetch.stderr:
                    raise GitError(self._mask(f"git fetch: {fetch.stderr.strip()}"))
                self._git("push", "origin", f"HEAD:{self.s.git_branch}")
                self.last_error = None
                self.last_sync = datetime.now(ZoneInfo(self.s.timezone)).strftime("%Y-%m-%d %H:%M")
                self._remember()
                return True
            except (GitError, OSError) as exc:
                self.last_error = self._mask(str(exc))
                log.error("Git: %s", self.last_error)
                self._remember()
                return False
            finally:
                # git удаляет пустые папки — папка для заметок должна существовать всегда.
                self.s.books_dir.mkdir(parents=True, exist_ok=True)

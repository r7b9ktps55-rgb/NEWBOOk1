"""Отправка заметок в Git-репозиторий хранилища Obsidian (по желанию)."""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import threading
from datetime import datetime

from .config import Settings

log = logging.getLogger(__name__)


class GitError(Exception):
    pass


class GitSync:
    def __init__(self, settings: Settings):
        self.s = settings
        self.vault = settings.vault
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self.last_sync: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.s.git_repo_url)

    def _mask(self, text: str) -> str:
        return text.replace(self.s.git_token, "***") if self.s.git_token else text

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["git"]
        if self.s.git_token:
            # Токен передаём только в заголовке команды — в .git/config он не попадает.
            basic = base64.b64encode(f"x-access-token:{self.s.git_token}".encode()).decode()
            cmd += ["-c", f"http.extraHeader=Authorization: Basic {basic}"]
        cmd += list(args)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        res = subprocess.run(cmd, cwd=self.vault, capture_output=True, text=True, timeout=180, env=env)
        if check and res.returncode != 0:
            raise GitError(self._mask(f"git {args[0]}: {(res.stderr or res.stdout).strip()}"))
        return res

    def _ensure_repo(self) -> None:
        if (self.vault / ".git").exists():
            return
        self.vault.mkdir(parents=True, exist_ok=True)
        self._git("init", "-b", self.s.git_branch)
        self._git("remote", "add", "origin", self.s.git_repo_url)
        self._git("config", "user.name", self.s.git_author_name)
        self._git("config", "user.email", self.s.git_author_email)
        # Если в репозитории уже есть хранилище — встаём поверх него, не трогая локальные файлы.
        if self._git("fetch", "origin", self.s.git_branch, check=False).returncode == 0:
            self._git("reset", f"origin/{self.s.git_branch}")
            # Заметки, которые есть только в репозитории, возвращаем на диск,
            # иначе следующий коммит их удалит.
            missing = self._git("ls-files", "--deleted", "-z").stdout.split("\0")
            missing = [f for f in missing if f]
            if missing:
                self._git("checkout", "--", *missing)

    def sync(self, message: str) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            try:
                self._ensure_repo()
                self._git("add", "-A")
                if self._git("diff", "--cached", "--quiet", check=False).returncode != 0:
                    self._git("commit", "-m", message)
                pull = self._git("pull", "--rebase", "origin", self.s.git_branch, check=False)
                if pull.returncode != 0 and "couldn't find remote ref" not in pull.stderr:
                    self._git("rebase", "--abort", check=False)
                    raise GitError(self._mask(f"git pull: {pull.stderr.strip()}"))
                self._git("push", "origin", f"HEAD:{self.s.git_branch}")
                self.last_error = None
                self.last_sync = datetime.now().strftime("%Y-%m-%d %H:%M")
                return True
            except (GitError, OSError, subprocess.TimeoutExpired) as exc:
                self.last_error = self._mask(str(exc))
                log.error("Git: %s", self.last_error)
                return False

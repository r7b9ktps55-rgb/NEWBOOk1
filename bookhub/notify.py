"""Уведомления на телефон о том, что что-то пошло не так (или готово).

NOTIFY_URL может быть:
- топиком ntfy: https://ntfy.sh/мой-секретный-топик (приложение ntfy на телефоне);
- любым вебхуком, принимающим POST с текстом;
- ссылкой с {text}, которая открывается GET-запросом, например Telegram:
  https://api.telegram.org/bot<токен>/sendMessage?chat_id=<id>&text={text}
"""

from __future__ import annotations

import logging
import re
import threading
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)


def mask_url(url: str) -> str:
    """Адрес для логов: без токенов в пути и логинов."""
    url = re.sub(r"//[^/@\s]+@", "//***@", url)
    url = re.sub(r"/bot[^/]+", "/bot***", url)
    return re.sub(r"(https?://[^/]+/).*", r"\1…", url)


class Notifier:
    def __init__(self, url: str = "", opener=urllib.request.urlopen, background: bool = True):
        self.url = url.strip()
        self.opener = opener
        self.background = background
        self.sent: list[str] = []  # для проверки в тестах

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def send(self, text: str) -> None:
        if not self.url:
            return
        text = f"Book Hub: {text}"[:1500]
        if self.background:
            threading.Thread(target=self._send, args=(text,), daemon=True).start()
        else:
            self._send(text)

    def _send(self, text: str) -> None:
        try:
            if "{text}" in self.url:
                req = urllib.request.Request(self.url.replace("{text}", urllib.parse.quote(text)))
            else:
                req = urllib.request.Request(self.url, data=text.encode(), method="POST",
                                             headers={"Content-Type": "text/plain; charset=utf-8", "Title": "Book Hub"})
            with self.opener(req, timeout=10) as resp:
                resp.read(200)
            self.sent.append(text)
        except Exception as exc:  # уведомление не должно ломать основную работу
            log.warning("Уведомление не отправлено на %s: %s", mask_url(self.url), exc.__class__.__name__)

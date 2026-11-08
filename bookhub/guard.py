"""Охрана входа: пароль, лимит неудачных попыток и размер запроса проверяются
до того, как сервер начнёт читать тело запроса. Без пароля на диск ничего не пишется."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import secrets
import time
from collections import deque
from html import escape

from fastapi.exceptions import HTTPException

log = logging.getLogger(__name__)

PUBLIC_PATHS = {"/health"}
PUBLIC_PREFIXES = ("/static/",)


class BodyTooLarge(HTTPException):
    """Тело запроса без Content-Length оказалось больше лимита.

    Наследник HTTPException: FastAPI пропускает такие ошибки при разборе формы как есть (с кодом 413),
    а любые другие превращает в 400."""

    def __init__(self) -> None:
        super().__init__(413, "Слишком большой запрос")


class LoginLimiter:
    """Не больше `attempts` неудачных входов с одного адреса за `window` секунд.

    Неудачи истекают только по времени: успешный вход с того же адреса их не обнуляет, иначе
    открытая вкладка владельца (она опрашивает сервер) сбрасывала бы счётчик подбирающему пароль.
    Каждая неудачная попытка ещё и отвечает с паузой `fail_delay`."""

    def __init__(self, attempts: int = 10, window: float = 600, clock=time.monotonic, fail_delay: float = 1.0):
        self.attempts = attempts
        self.window = window
        self.clock = clock
        self.fail_delay = fail_delay
        self._fails: dict[str, deque] = {}

    def _recent(self, ip: str) -> deque | None:
        q = self._fails.get(ip)
        if q is None:
            return None
        now = self.clock()
        while q and now - q[0] > self.window:
            q.popleft()
        if not q:
            del self._fails[ip]
            return None
        return q

    def blocked(self, ip: str) -> bool:
        q = self._recent(ip)
        return q is not None and len(q) >= self.attempts

    def fail(self, ip: str) -> None:
        if len(self._fails) > 10_000:  # защита памяти от перебора с множества адресов
            self._fails.clear()
        self._fails.setdefault(ip, deque()).append(self.clock())

    def success(self, ip: str) -> None:
        """Ничего не обнуляет: неудачи истекают по окну времени (см. описание класса)."""


def check_basic(header: bytes | None, user: str, password: str) -> bool | None:
    """None — логин не передан, False — неверный, True — верный."""
    if not header:
        return None
    try:
        scheme, _, value = header.decode("latin-1").partition(" ")
        if scheme.lower() != "basic":
            return False
        decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return False
    login, sep, pwd = decoded.partition(":")
    if not sep:
        return False
    ok_user = secrets.compare_digest(login.encode(), user.encode())
    ok_pass = secrets.compare_digest(pwd.encode(), password.encode())
    return ok_user and ok_pass


class GuardMiddleware:
    def __init__(self, app, *, user: str, password: str, max_body: int, limiter: LoginLimiter):
        self.app = app
        self.user = user
        self.password = password
        self.max_body = max_body
        self.limiter = limiter

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        ip = (scope.get("client") or ("unknown", 0))[0]
        if self.limiter.blocked(ip):
            return await reply(send, 429, "Слишком много неудачных попыток входа. Подождите 10 минут.",
                               [(b"retry-after", b"600")])
        ok = check_basic(headers.get(b"authorization"), self.user, self.password)
        if not ok:
            if ok is False:
                self.limiter.fail(ip)
                log.warning("Неудачная попытка входа с адреса %s", ip)
                if self.limiter.fail_delay:
                    await asyncio.sleep(self.limiter.fail_delay)  # замедляем подбор пароля
            return await reply(send, 401, "Нужны логин и пароль.",
                               [(b"www-authenticate", b'Basic realm="Book Hub", charset="UTF-8"')])
        self.limiter.success(ip)
        if scope.get("method") in ("POST", "PUT", "PATCH"):
            raw = headers.get(b"content-length")
            if raw is None:
                # Тело без длины (chunked): считаем байты по мере чтения и прерываем на лимите.
                receive = self._limited(receive)
            else:
                try:
                    size = int(raw)
                except ValueError:
                    return await reply(send, 400, "Неверный заголовок Content-Length.")
                if size > self.max_body:
                    mb = self.max_body // (1024 * 1024)
                    return await reply(send, 413, f"Слишком большой запрос: больше {mb} МБ за один раз.")
        await self.app(scope, receive, send)

    def _limited(self, receive):
        total = 0

        async def wrapped():
            nonlocal total
            message = await receive()
            if message.get("type") == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_body:
                    raise BodyTooLarge()
            return message

        return wrapped


async def reply(send, status: int, text: str, extra: list[tuple[bytes, bytes]] | None = None) -> None:
    body = f'<!doctype html><meta charset="utf-8"><title>Book Hub</title><p>{escape(text)}</p>'.encode()
    headers = [
        (b"content-type", b"text/html; charset=utf-8"),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
        (b"x-content-type-options", b"nosniff"),
        (b"content-security-policy", b"default-src 'none'"),
        *(extra or []),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})

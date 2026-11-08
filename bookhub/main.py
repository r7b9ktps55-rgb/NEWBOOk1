"""Веб-интерфейс Book Hub. Запуск: uvicorn bookhub.main:create_app --factory"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import urllib.parse
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import markdown as md
import nh3
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from .config import Settings
from .extract import FORMATS as FILE_TYPES, looks_valid, split_name
from .formats import FORMATS, parse_formats
from .gitsync import GitSync
from .guard import GuardMiddleware, LoginLimiter
from .library import find_book, library, parse_meta
from .notify import Notifier
from .store import JobStore
from .summarizer import GeminiSummarizer, Summarizer, safe_filename
from .trash import Trash
from .worker import Processor

log = logging.getLogger("bookhub")
mimetypes.add_type("application/manifest+json", ".webmanifest")
HERE = Path(__file__).parent
OBSIDIAN_URI_LIMIT = 60_000

# Сообщения после действий: (текст, вид). «ok» — подтверждение, «warn» — что-то не так.
MESSAGES = {
    "added": ("Добавлено в очередь", "ok"),
    "rejected": ("Некоторые файлы пропущены: формат не поддерживается, файл повреждён или больше лимита", "warn"),
    "nothing": ("Выберите файл книги или вставьте ссылку", "warn"),
    "badurl": ("Нужна ссылка вида https://…", "warn"),
    "more": ("Новые форматы добавлены в очередь", "ok"),
    "nosource": ("Исходник этой книги не найден — загрузите файл книги заново", "warn"),
    "started": ("Обработка запущена — очередь обновится сама", "ok"),
    "busy": ("Уже идёт обработка, подождите", "warn"),
    "nokey": ("Не задан GEMINI_API_KEY — конспекты делать нечем", "warn"),
    "empty": ("Очередь пуста", "warn"),
    "retry": ("Задача снова в очереди", "ok"),
    "deleted": ("Удалено — файл лежит в корзине", "ok"),
    "restored": ("Восстановлено из корзины", "ok"),
    "trash_emptied": ("Корзина очищена", "ok"),
}

# Подписи полей frontmatter на странице заметки (title, author, book и format показываются в заголовке)
PROP_LABELS = {"created": "Создано", "source": "Источник", "status": "Статус", "tags": "Теги"}
STATUS_LABELS = {"unread": "не прочитано", "reading": "читаю", "read": "прочитано"}

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


# Markdown без attr_list и md_in_html: из текста заметки нельзя добавить атрибуты или сырой HTML.
MD_EXTENSIONS = ["abbr", "def_list", "fenced_code", "footnotes", "tables", "sane_lists", "nl2br"]
SAFE_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "b", "i", "del", "s", "sup", "sub",
    "code", "pre", "blockquote", "ul", "ol", "li", "dl", "dt", "dd", "table", "thead", "tbody", "tr", "th", "td",
    "a", "span", "div", "abbr",
}
SAFE_ATTRS = {"a": {"href", "title"}, "abbr": {"title"}, "th": {"align"}, "td": {"align"}, "sup": {"id"}, "li": {"id"}}
SAFE_CLASSES = {"span": {"wikilink", "callout"}, "a": {"footnote-ref", "footnote-backref"}, "div": {"footnote"}}


_render_cache: dict[tuple[str, int, int], tuple[str, str]] = {}


def render_note_cached(path: Path, text: str) -> tuple[str, str]:
    """Отрисованная заметка кэшируется до изменения файла (ключ — путь, mtime и размер)."""
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    hit = _render_cache.get(key)
    if hit is None:
        if len(_render_cache) > 64:
            _render_cache.clear()
        hit = _render_cache[key] = render_note(text)
    return hit


def render_note(text: str) -> tuple[str, str]:
    """Возвращает (frontmatter, html). Сырой HTML из заметки не пропускаем."""
    front = ""
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            front, text = text[4:end].strip(), text[end + 4:].lstrip("\n")
    text = text.replace("&", "&amp;").replace("<", "&lt;")
    # Карточки «Вопрос::Ответ» показываем списком
    text = re.sub(r"^(?![\s>#*-])(.+?)::(.+)$", r"- **\1** — \2", text, flags=re.M)
    text = re.sub(r"^(\s*[-*]) \[ \]", r"\1 ☐", text, flags=re.M)
    text = re.sub(r"^(\s*[-*]) \[[xX]\]", r"\1 ☑", text, flags=re.M)
    html = md.markdown(text, extensions=MD_EXTENSIONS)
    html = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: f'<span class="wikilink">{m.group(2) or m.group(1)}</span>', html)
    html = re.sub(r"\[!(\w+)\]", r'<span class="callout">\1</span>', html)
    # Последний рубеж: только разрешённые теги, атрибуты и ссылки http(s)/mailto.
    html = nh3.clean(html, tags=SAFE_TAGS, attributes=SAFE_ATTRS, allowed_classes=SAFE_CLASSES,
                     url_schemes={"http", "https", "mailto"})
    return front, html


def note_props(meta: dict[str, str]) -> list[tuple[str, str]]:
    """Свойства заметки для показа: понятные подписи вместо ключей frontmatter."""
    props = []
    for key, label in PROP_LABELS.items():
        value = meta.get(key, "")
        if key == "status":
            value = STATUS_LABELS.get(value, value)
        if key == "tags":
            value = value.strip("[]")
        if value:
            props.append((label, value))
    if meta.get("words") and meta.get("source_words"):
        props.append(("Объём", f"≈ {int(meta['words']):,} слов из {int(meta['source_words']):,}".replace(",", " ")))
    hidden = {*PROP_LABELS, "title", "author", "book", "format", "words", "source_words"}
    props += [(k, v) for k, v in meta.items() if k not in hidden and v]
    return props


MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]


def human_when(value: str | None, tz) -> str:
    """«2026-11-08 09:00» → «сегодня в 09:00», «вчера в 09:00» или «6 ноября в 09:00»."""
    if not value:
        return ""
    try:
        moment = datetime.strptime(value[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return value
    today = datetime.now(tz).date()
    day = moment.date()
    if day == today:
        prefix = "сегодня"
    elif (today - day).days == 1:
        prefix = "вчера"
    else:
        prefix = f"{day.day} {MONTHS[day.month - 1]}" + ("" if day.year == today.year else f" {day.year}")
    return f"{prefix} в {moment:%H:%M}"


def static_version(folder: Path) -> str:
    """Меняется при любом изменении файлов в static/ — чтобы браузер не держал старые стили и скрипты."""
    h = hashlib.sha1()
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            st = p.stat()
            h.update(f"{p.relative_to(folder)}:{st.st_size}:{st.st_mtime_ns}".encode())
    return h.hexdigest()[:10]


class _HideHealthChecks(logging.Filter):
    """Проверка здоровья раз в 30 секунд не должна засорять журнал."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/health" not in record.getMessage()


def setup_logging(s: Settings) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    tz = s.tz
    # Время в журнале — местное. staticmethod: иначе Python передаст функции ещё и сам форматтер.
    logging.Formatter.converter = staticmethod(lambda ts: datetime.fromtimestamp(ts, tz).timetuple())
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _HideHealthChecks) for f in access.filters):
        access.addFilter(_HideHealthChecks())


def acquire_scheduler_lock(s: Settings):
    """Расписание должно работать в одном процессе, даже если запущено несколько копий приложения."""
    try:
        import fcntl
    except ImportError:  # Windows: блокировки нет, полагаемся на один процесс
        return True
    handle = open(s.data_dir / ".scheduler.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        return None


def create_app(settings: Settings | None = None, summarizer: Summarizer | None = None,
               start_scheduler: bool = True) -> FastAPI:
    s = settings or Settings.from_env()
    s.validate()
    setup_logging(s)
    s.ensure_dirs()

    if summarizer is None and s.gemini_api_key:
        summarizer = GeminiSummarizer(s.gemini_api_key, s.gemini_model, s.data_dir)
    store = JobStore(s.db_path)
    notifier = Notifier(s.notify_url)
    trash = Trash(s)
    git = GitSync(s, store)
    proc = Processor(s, store, summarizer, git, notifier, trash)
    if isinstance(summarizer, GeminiSummarizer):
        summarizer.max_requests = s.max_requests_per_job
        summarizer.meter = proc.take_request
    tasks: set[asyncio.Task] = set()

    def missed_daily_run() -> bool:
        """Сервер был выключен в назначенное время, а разбор уже когда-то работал — догоняем."""
        hm = s.daily_time()
        raw = store.get_meta("last_daily_run")
        if not hm or not raw:
            return False
        now = datetime.now(s.tz)
        last = json.loads(raw).get("at", "")[:10]
        return now >= now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0) and last < now.strftime("%Y-%m-%d")

    async def maintenance_job() -> None:
        await asyncio.to_thread(proc.maintenance)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sched = None
        lock = acquire_scheduler_lock(s) if start_scheduler else None
        if lock:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler

            # coalesce + misfire_grace_time: если машина спала в назначенное время, задача выполнится после пробуждения.
            sched = AsyncIOScheduler(timezone=s.timezone, job_defaults={"coalesce": True, "misfire_grace_time": 6 * 3600})
            hm, bt = s.daily_time(), s.backup_at()
            if hm:
                sched.add_job(proc.daily, "cron", hour=hm[0], minute=hm[1], id="daily")
                log.info("Ежедневный разбор: %s (%s), по %d шт.", s.daily_run_time, s.timezone, s.daily_batch_size)
            if bt:
                sched.add_job(maintenance_job, "cron", hour=bt[0], minute=bt[1], id="maintenance")
            sched.start()
            if missed_daily_run():
                log.info("Ежедневный разбор был пропущен — запущу его через минуту")
                sched.add_job(proc.daily, "date", run_date=datetime.now(s.tz) + timedelta(minutes=1), id="catch-up")
        elif start_scheduler:
            log.warning("Расписание уже работает в другом процессе приложения — здесь оно выключено")
        yield
        if sched:
            sched.shutdown(wait=False)
        if lock and lock is not True:
            lock.close()
        for t in tasks:
            t.cancel()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["asset_v"] = static_version(HERE / "static")
    templates.env.filters["when"] = lambda value: human_when(value, s.tz)
    security = HTTPBasic(realm="Book Hub")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        path = request.url.path
        if path.startswith("/static/") and resp.status_code == 200:
            if "v" in request.query_params or path.startswith("/static/vendor/"):
                # Версия в адресе (?v=… или в имени файла) — содержимое по этому адресу не меняется никогда.
                resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                resp.headers["Cache-Control"] = "public, max-age=604800"
        elif path == "/live":
            resp.headers["Cache-Control"] = "no-store"
        elif "cache-control" not in resp.headers:
            # no-cache, а не no-store: браузер может вернуть страницу кнопкой «Назад» мгновенно (bfcache),
            # но каждый новый показ всё равно сверяет её с сервером.
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    ERROR_TITLES = {400: "Неверный запрос", 403: "Доступ запрещён", 404: "Не найдено", 405: "Так нельзя",
                    413: "Слишком большой файл", 422: "Неверные данные", 500: "Что-то пошло не так"}

    def wants_html(request: Request) -> bool:
        return "text/html" in request.headers.get("accept", "")

    def error_page(request: Request, status: int, detail: str, headers=None):
        if not wants_html(request):
            return JSONResponse({"detail": detail}, status_code=status, headers=headers)
        return templates.TemplateResponse(request, "error.html", {
            "status": status, "title": ERROR_TITLES.get(status, "Ошибка"), "detail": detail,
        }, status_code=status, headers=headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "Ошибка"
        detail = {"Not Found": "Такой страницы нет.", "Method Not Allowed": "Такое действие здесь недоступно."}.get(detail, detail)
        return error_page(request, exc.status_code, detail, getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return error_page(request, 422, "Форма заполнена неверно — вернитесь и попробуйте ещё раз.")

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception):
        log.exception("Необработанная ошибка на %s", request.url.path)
        return error_page(request, 500, "Ошибка на сервере. Подробности — в журнале (docker compose logs).")

    app.add_middleware(GZipMiddleware, minimum_size=1024)
    # Добавляется последним, значит срабатывает первым: до разбора формы и записи файлов.
    limiter = LoginLimiter()
    app.add_middleware(GuardMiddleware, user=s.web_user, password=s.web_password,
                       max_body=s.max_request_bytes, limiter=limiter)

    def auth(request: Request, c: HTTPBasicCredentials = Depends(security)) -> str:
        ok_user = secrets.compare_digest(c.username.encode(), s.web_user.encode())
        ok_pass = secrets.compare_digest(c.password.encode(), s.web_password.encode())
        if not (ok_user and ok_pass):
            raise HTTPException(401, "Неверный логин или пароль", headers={"WWW-Authenticate": 'Basic realm="Book Hub"'})
        # Защита от CSRF: формы принимаем только со своего же сайта.
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and urllib.parse.urlsplit(origin).netloc != request.headers.get("host"):
                raise HTTPException(403, "Запрос с чужого сайта")
        return c.username

    def back(msg: str, **extra: str) -> RedirectResponse:
        query = urllib.parse.urlencode({"m": msg, **extra})
        return RedirectResponse(f"/?{query}", status_code=303)

    def note_path(name: str) -> Path:
        base = s.books_dir.resolve()
        try:
            path = (base / name).resolve()
        except (ValueError, OSError):  # например, нулевой байт в имени
            raise HTTPException(404, "Заметка не найдена") from None
        if "\x00" in name or path.parent != base or path.suffix != ".md" or not path.is_file():
            raise HTTPException(404, "Заметка не найдена")
        return path

    def start(limit: int | None) -> str:
        if not summarizer:
            return "nokey"
        if proc.busy:
            return "busy"
        if not store.list("queued") and not any(split_name(p.name)[1] for p in s.inbox.iterdir()):
            return "empty"
        t = asyncio.create_task(proc.run(limit))
        tasks.add(t)
        t.add_done_callback(tasks.discard)
        return "started"

    def health_checks() -> dict[str, bool]:
        checks = {}
        try:
            store.get_meta("health")
            checks["db"] = True
        except Exception:
            checks["db"] = False
        checks["disk"] = shutil.disk_usage(s.data_dir).free > 200 * 1024 * 1024
        stale = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds")
        checks["queue"] = not any(j.updated_at < stale for j in store.list("processing")) if checks["db"] else False
        return checks

    @app.get("/health")
    async def health():
        """Для Docker: 503, если база недоступна, диск почти заполнен или задача зависла."""
        checks = await asyncio.to_thread(health_checks)
        ok = all(checks.values())
        return JSONResponse({"ok": ok, **checks}, status_code=200 if ok else 503)

    def page_state() -> dict:
        """Всё, что нужно очереди и библиотеке; version меняется при любом изменении."""
        jobs = store.list()
        active = [j for j in jobs if j.status != "done"]
        books = library(s.books_dir)
        busy = proc.busy or any(j.status == "processing" for j in jobs)
        queue_sig = repr([(j.id, j.status, j.progress, j.done_formats, j.error) for j in active]) + repr(busy)
        trash_count = len(trash.items())
        lib_sig = repr([(b.name, [n.name for n in b.notes], round(b.mtime, 3)) for b in books]) + f"|{trash_count}"
        lib_version = hashlib.sha1(lib_sig.encode()).hexdigest()[:12]
        today_requests = store.requests_on(proc.today())
        last_daily = json.loads(store.get_meta("last_daily_run") or "null")
        return {
            "s": s,
            "git": git,
            "formats": FORMATS,
            "has_key": summarizer is not None,
            "requests_today": today_requests,
            "limit_reached": bool(s.daily_request_limit) and today_requests >= s.daily_request_limit,
            "last_daily": last_daily,
            "trash_count": trash_count,
            "notifications": notifier.enabled,
            "active": active,
            "books": books,
            "busy": busy,
            "notes_total": sum(len(b.notes) for b in books),
            "lib_version": lib_version,
            "version": hashlib.sha1((queue_sig + lib_version + str(git.last_error) + str(today_requests)
                                     + str(last_daily)).encode()).hexdigest()[:12],
        }

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, m: str = "", undo: str = "", _: str = Depends(auth)):
        await asyncio.to_thread(proc.sync_inbox)
        state = await asyncio.to_thread(page_state)
        text, kind = MESSAGES.get(m, ("", ""))
        types = list(FILE_TYPES.values())
        return templates.TemplateResponse(request, "index.html", {
            **state,
            "message": text,
            "message_kind": kind,
            "undo": undo if m == "deleted" and re.fullmatch(r"[0-9]{14}-[0-9a-f]{6}", undo) else "",
            "checked": s.default_formats,
            "file_types": sorted(set(types), key=types.index),
            "file_exts": list(FILE_TYPES),
        })

    @app.get("/live")
    async def live(v: str = "", lib: str = "", _: str = Depends(auth)):
        """Состояние очереди для обновления страницы без перезагрузки.

        Клиент присылает версии того, что у него уже есть; неизменившиеся куски не пересылаются."""
        await asyncio.to_thread(proc.sync_inbox)
        state = await asyncio.to_thread(page_state)
        queue_html = None
        if v != state["version"]:
            queue_html = templates.env.get_template("_queue.html").render(state)
        library_html = None
        if lib != state["lib_version"]:
            library_html = templates.env.get_template("_library.html").render(state)
        return JSONResponse({
            "version": state["version"],
            "lib_version": state["lib_version"],
            "active": len(state["active"]),
            "busy": state["busy"],
            "notes": state["notes_total"],
            "queue": queue_html,
            "library": library_html,
        })

    async def save_file(f: UploadFile, picked: list[str]) -> Path | None:
        """Сохраняет книгу в inbox. None — если формат не поддерживается, файл битый или больше лимита."""
        limit = s.max_upload_mb * 1024 * 1024
        raw_stem, ext = split_name(Path(f.filename or "").name)
        if not ext:
            return None
        # Сначала пишем во временный файл с уникальным именем: две одновременные загрузки не столкнутся.
        tmp = s.uploads_tmp / f"{uuid.uuid4().hex}.part"
        size, ok = 0, True
        with tmp.open("wb") as out:
            while chunk := await f.read(1024 * 1024):
                if size == 0 and not looks_valid(ext, chunk[:4096]):
                    ok = False
                    break
                size += len(chunk)
                if size > limit:
                    ok = False
                    break
                out.write(chunk)
        if not (ok and size):
            tmp.unlink(missing_ok=True)
            return None

        def place() -> Path:
            with proc.inbox_lock:  # имя выбирается и занимается атомарно (подхват inbox ждёт)
                dest = proc.free_inbox_name(safe_filename(raw_stem, "book"), ext)
                os.replace(tmp, dest)
                store.add("file", dest.name, split_name(dest.name)[0], picked)
                return dest

        return await asyncio.to_thread(place)

    @app.post("/add")
    async def add(files: list[UploadFile] = File([]), url: str = Form(""), formats: list[str] = Form([]),
                  _: str = Depends(auth)):
        picked = parse_formats(formats, list(s.default_formats))
        files = [f for f in files if f.filename]
        url = url.strip()
        if not files and not url:
            return back("nothing")
        rejected = 0
        for f in files:
            if not await save_file(f, picked):
                rejected += 1
        if url:
            parts = urllib.parse.urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.netloc or len(url) > 2000:
                return back("badurl")
            store.add("url", url, url, picked)
        return back("rejected" if rejected else "added")

    @app.post("/run")
    async def run(all: str = Form(""), _: str = Depends(auth)):
        return back(start(None if all else 1))

    @app.post("/jobs/{job_id}/retry")
    async def retry(job_id: int, _: str = Depends(auth)):
        store.requeue(job_id)
        return back("retry")

    @app.post("/jobs/{job_id}/delete")
    async def delete(job_id: int, _: str = Depends(auth)):
        job = store.delete(job_id)
        undo = ""
        # Файл ещё не взятой в работу книги лежит в inbox — переносим его в корзину, а не стираем.
        if job and job.is_file and job.status == "queued":
            path = s.inbox / Path(job.source).name
            if path.is_file():
                undo = trash.put(path)
        return back("deleted", undo=undo) if undo else back("deleted")

    @app.get("/notes/{name}", response_class=HTMLResponse)
    async def view_note(request: Request, name: str, _: str = Depends(auth)):
        path = note_path(name)
        text = path.read_text("utf-8")
        front, html = render_note_cached(path, text)
        file_in_vault = path.relative_to(s.vault.resolve()).with_suffix("").as_posix()
        params = {"file": file_in_vault, "content": text}
        if s.obsidian_vault_name:
            params = {"vault": s.obsidian_vault_name, **params}
        uri = "obsidian://new?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        book = await asyncio.to_thread(find_book, s.books_dir, path.name)
        meta = parse_meta(text)
        fmt = FORMATS.get(meta.get("format", ""), FORMATS["summary"] if meta else None)
        return templates.TemplateResponse(request, "note.html", {
            "heading": meta.get("title") or path.stem,
            "author": meta.get("author", ""),
            "format_label": fmt.label if fmt else "Заметка",
            "props": note_props(meta),
            "name": path.name, "front": front, "html": html,
            "obsidian_uri": uri if len(uri) <= OBSIDIAN_URI_LIMIT else "",
            "book": book, "formats": FORMATS, "has_key": summarizer is not None,
        })

    @app.post("/notes/{name}/more")
    async def more_formats(name: str, formats: list[str] = Form([]), _: str = Depends(auth)):
        note_path(name)
        book = find_book(s.books_dir, name)
        picked = [f for f in parse_formats(formats, []) if not book or f not in book.formats]
        if not book or not picked:
            return back("nothing")
        src = book.source
        if src.startswith(("http://", "https://")):
            store.add("url", src, book.name, picked, book=book.name)
        elif src and proc.find_file(src):
            store.add("file", Path(src).name, book.name, picked, book=book.name)
        else:
            return back("nosource")
        return back("more")

    @app.get("/notes/{name}/download")
    async def download(name: str, _: str = Depends(auth)):
        path = note_path(name)
        return FileResponse(path, filename=path.name, media_type="text/markdown; charset=utf-8")

    def sync_later(message: str) -> None:
        if git.enabled:
            t = asyncio.create_task(asyncio.to_thread(git.sync, message))
            tasks.add(t)
            t.add_done_callback(tasks.discard)

    @app.post("/notes/{name}/delete")
    async def delete_note(name: str, _: str = Depends(auth)):
        item = trash.put(note_path(name))
        sync_later(f"Remove {Path(name).stem}")
        return back("deleted", undo=item)

    @app.post("/trash/{item_id}/restore")
    async def restore(item_id: str, _: str = Depends(auth)):
        restored = await asyncio.to_thread(trash.restore, item_id)
        if not restored:
            raise HTTPException(404, "В корзине такого нет")
        if restored.parent == s.books_dir.resolve():
            sync_later(f"Restore {restored.stem}")
        return back("restored")

    @app.post("/trash/empty")
    async def empty_trash(_: str = Depends(auth)):
        await asyncio.to_thread(trash.purge)
        return back("trash_emptied")

    @app.get("/export.zip")
    async def export_notes(_: str = Depends(auth)):
        """Все заметки одним архивом — например, чтобы перенести их в другое хранилище."""
        def build() -> bytes:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for p in sorted(s.books_dir.glob("*.md")):
                    z.write(p, f"{s.vault_subfolder or 'Books'}/{p.name}")
            return buf.getvalue()

        data = await asyncio.to_thread(build)
        stamp = datetime.now(s.tz).strftime("%Y-%m-%d")
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="bookhub-notes-{stamp}.zip"'})

    app.state.store, app.state.processor, app.state.git, app.state.limiter = store, proc, git, limiter
    app.state.trash, app.state.notifier = trash, notifier
    return app

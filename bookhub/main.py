"""Веб-интерфейс Book Hub. Запуск: uvicorn bookhub.main:create_app --factory"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

import markdown as md
import nh3
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.gzip import GZipMiddleware

from .config import Settings
from .extract import FORMATS as FILE_TYPES, looks_valid, split_name
from .formats import FORMATS, parse_formats
from .gitsync import GitSync
from .guard import GuardMiddleware, LoginLimiter
from .library import find_book, library, parse_meta
from .store import JobStore
from .summarizer import GeminiSummarizer, Summarizer, safe_filename
from .worker import Processor

log = logging.getLogger("bookhub")
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
    "deleted": ("Удалено", "ok"),
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


def static_version(folder: Path) -> str:
    """Меняется при любом изменении файлов в static/ — чтобы браузер не держал старые стили и скрипты."""
    h = hashlib.sha1()
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            st = p.stat()
            h.update(f"{p.relative_to(folder)}:{st.st_size}:{st.st_mtime_ns}".encode())
    return h.hexdigest()[:10]


def create_app(settings: Settings | None = None, summarizer: Summarizer | None = None,
               start_scheduler: bool = True) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = settings or Settings.from_env()
    s.validate()
    s.ensure_dirs()

    if summarizer is None and s.gemini_api_key:
        summarizer = GeminiSummarizer(s.gemini_api_key, s.gemini_model, s.data_dir)
    store = JobStore(s.db_path)
    git = GitSync(s)
    proc = Processor(s, store, summarizer, git)
    tasks: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sched = None
        hm = s.daily_time()
        if start_scheduler and hm:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler

            sched = AsyncIOScheduler(timezone=s.timezone)
            sched.add_job(proc.run, "cron", hour=hm[0], minute=hm[1], kwargs={"limit": s.daily_batch_size})
            sched.start()
            log.info("Ежедневный разбор: %s (%s), по %d шт.", s.daily_run_time, s.timezone, s.daily_batch_size)
        yield
        if sched:
            sched.shutdown(wait=False)
        for t in tasks:
            t.cancel()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["asset_v"] = static_version(HERE / "static")
    security = HTTPBasic(realm="Book Hub")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/static/") and resp.status_code == 200:
            # Стили и скрипты подключаются с ?v=<версия>, поэтому их можно кэшировать надолго.
            resp.headers["Cache-Control"] = "public, max-age=604800"
        elif "cache-control" not in resp.headers:
            resp.headers["Cache-Control"] = "no-store"
        return resp

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

    def back(msg: str) -> RedirectResponse:
        return RedirectResponse(f"/?m={msg}", status_code=303)

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

    @app.get("/health")
    async def health():
        return JSONResponse({"ok": True})

    def page_state() -> dict:
        """Всё, что нужно очереди и библиотеке; version меняется при любом изменении."""
        jobs = store.list()
        active = [j for j in jobs if j.status != "done"]
        books = library(s.books_dir)
        busy = proc.busy or any(j.status == "processing" for j in jobs)
        queue_sig = repr([(j.id, j.status, j.progress, j.done_formats, j.error) for j in active]) + repr(busy)
        lib_sig = repr([(b.name, [n.name for n in b.notes], round(b.mtime, 3)) for b in books])
        lib_version = hashlib.sha1(lib_sig.encode()).hexdigest()[:12]
        return {
            "s": s,
            "git": git,
            "formats": FORMATS,
            "has_key": summarizer is not None,
            "active": active,
            "books": books,
            "busy": busy,
            "notes_total": sum(len(b.notes) for b in books),
            "lib_version": lib_version,
            "version": hashlib.sha1((queue_sig + lib_version + str(git.last_error)).encode()).hexdigest()[:12],
        }

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, m: str = "", _: str = Depends(auth)):
        await asyncio.to_thread(proc.sync_inbox)
        state = await asyncio.to_thread(page_state)
        text, kind = MESSAGES.get(m, ("", ""))
        types = list(FILE_TYPES.values())
        return templates.TemplateResponse(request, "index.html", {
            **state,
            "message": text,
            "message_kind": kind,
            "checked": s.default_formats,
            "file_types": sorted(set(types), key=types.index),
            "file_exts": list(FILE_TYPES),
        })

    @app.get("/live")
    async def live(lib: str = "", _: str = Depends(auth)):
        """Состояние очереди для обновления страницы без перезагрузки."""
        await asyncio.to_thread(proc.sync_inbox)
        state = await asyncio.to_thread(page_state)
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

    async def save_file(f: UploadFile) -> Path | None:
        """Сохраняет книгу в inbox. None — если формат не поддерживается, файл битый или больше лимита."""
        limit = s.max_upload_mb * 1024 * 1024
        raw_stem, ext = split_name(Path(f.filename or "").name)
        if not ext:
            return None
        stem = safe_filename(raw_stem, "book")
        # Имя не должно совпасть ни с файлом в inbox, ни с уже обработанной книгой.
        dest, n = s.inbox / f"{stem}{ext}", 2
        while dest.exists() or store.has_file(dest.name):
            dest, n = s.inbox / f"{stem} ({n}){ext}", n + 1
        tmp = s.inbox / f".{dest.name}.part"
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
        if ok and size:
            tmp.rename(dest)
            return dest
        tmp.unlink(missing_ok=True)
        return None

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
            dest = await save_file(f)
            if dest:
                store.add("file", dest.name, split_name(dest.name)[0], picked)
            else:
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
        if job and job.is_file and job.status != "done":
            (s.inbox / Path(job.source).name).unlink(missing_ok=True)
        return back("deleted")

    @app.get("/notes/{name}", response_class=HTMLResponse)
    async def view_note(request: Request, name: str, _: str = Depends(auth)):
        path = note_path(name)
        text = path.read_text("utf-8")
        front, html = render_note(text)
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
            "name": path.name, "front": front, "html": html, "raw": text,
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

    @app.post("/notes/{name}/delete")
    async def delete_note(name: str, _: str = Depends(auth)):
        note_path(name).unlink()
        if git.enabled:
            t = asyncio.create_task(asyncio.to_thread(git.sync, f"Remove {Path(name).stem}"))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
        return back("deleted")

    app.state.store, app.state.processor, app.state.git, app.state.limiter = store, proc, git, limiter
    return app

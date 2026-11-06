"""Веб-интерфейс Book Hub. Запуск: uvicorn bookhub.main:create_app --factory"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

import markdown as md
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Settings
from .formats import FORMATS, parse_formats
from .gitsync import GitSync
from .library import find_book, library
from .store import JobStore
from .summarizer import GeminiSummarizer, Summarizer, safe_filename
from .worker import Processor

log = logging.getLogger("bookhub")
HERE = Path(__file__).parent
OBSIDIAN_URI_LIMIT = 60_000

MESSAGES = {
    "added": "Добавлено в очередь",
    "rejected": "Некоторые файлы пропущены: нужен PDF не больше лимита",
    "nothing": "Выберите PDF или вставьте ссылку",
    "badurl": "Нужна ссылка вида https://…",
    "more": "Новые форматы добавлены в очередь",
    "nosource": "Исходник этой книги не найден — загрузите PDF заново",
    "started": "Обработка запущена — страница обновится сама",
    "busy": "Уже идёт обработка, подождите",
    "nokey": "Не задан GEMINI_API_KEY — конспекты делать нечем",
    "empty": "Очередь пуста",
    "retry": "Задача снова в очереди",
    "deleted": "Удалено",
}

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


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
    html = md.markdown(text, extensions=["extra", "sane_lists", "nl2br"])
    html = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: f'<span class="wikilink">{m.group(2) or m.group(1)}</span>', html)
    html = re.sub(r"\[!(\w+)\]", r'<span class="callout">\1</span>', html)
    return front, html


def create_app(settings: Settings | None = None, summarizer: Summarizer | None = None,
               start_scheduler: bool = True) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = settings or Settings.from_env()
    s.validate()
    s.ensure_dirs()
    if s.web_password in ("admin", "password", "123456"):
        log.warning("WEB_PASSWORD слишком простой — смените его, если сайт доступен из интернета")

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
    security = HTTPBasic(realm="Book Hub")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        return resp

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
        path = (base / name).resolve()
        if path.parent != base or path.suffix != ".md" or not path.is_file():
            raise HTTPException(404, "Заметка не найдена")
        return path

    def start(limit: int | None) -> str:
        if not summarizer:
            return "nokey"
        if proc.busy:
            return "busy"
        if not store.list("queued") and not any(p.suffix.lower() == ".pdf" for p in s.inbox.iterdir()):
            return "empty"
        t = asyncio.create_task(proc.run(limit))
        tasks.add(t)
        t.add_done_callback(tasks.discard)
        return "started"

    @app.get("/health")
    async def health():
        return JSONResponse({"ok": True})

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, m: str = "", _: str = Depends(auth)):
        await asyncio.to_thread(proc.sync_inbox)
        jobs = store.list()
        active = [j for j in jobs if j.status != "done"]
        return templates.TemplateResponse(request, "index.html", {
            "s": s,
            "message": MESSAGES.get(m, ""),
            "active": active,
            "books": await asyncio.to_thread(library, s.books_dir),
            "formats": FORMATS,
            "checked": s.default_formats,
            "busy": proc.busy or any(j.status == "processing" for j in jobs),
            "has_key": summarizer is not None,
            "git": git,
        })

    async def save_pdf(f: UploadFile) -> Path | None:
        """Сохраняет PDF в inbox. None — если это не PDF или он больше лимита."""
        limit = s.max_upload_mb * 1024 * 1024
        if not (f.filename or "").lower().endswith(".pdf"):
            return None
        stem = safe_filename(Path(f.filename or "").stem, "book")
        # Имя не должно совпасть ни с файлом в inbox, ни с уже обработанной книгой.
        dest, n = s.inbox / f"{stem}.pdf", 2
        while dest.exists() or store.has_source("pdf", dest.name):
            dest, n = s.inbox / f"{stem} ({n}).pdf", n + 1
        tmp = dest.with_suffix(".part")
        size, ok = 0, True
        with tmp.open("wb") as out:
            while chunk := await f.read(1024 * 1024):
                if size == 0 and not chunk.startswith(b"%PDF"):
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
            dest = await save_pdf(f)
            if dest:
                store.add("pdf", dest.name, dest.stem, picked)
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
        if job and job.kind == "pdf" and job.status != "done":
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
        return templates.TemplateResponse(request, "note.html", {
            "title": path.stem, "name": path.name, "front": front, "html": html, "raw": text,
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
        elif src and proc.find_pdf(src):
            store.add("pdf", Path(src).name, book.name, picked, book=book.name)
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

    app.state.store, app.state.processor, app.state.git = store, proc, git
    return app

"""Заметки через Gemini: PDF целиком или текст статьи по ссылке, в разных форматах."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterator, Protocol

from .extract import extract_text, split_name
from .formats import FORMATS, HEADER, compress_prompt

log = logging.getLogger(__name__)

MAX_URL_TEXT = 400_000
MAX_BOOK_TEXT = 2_000_000  # ~0,5–0,7 млн токенов: с запасом влезает в контекст Gemini
UPLOAD_TIMEOUT_S = 600
CHUNK_WORDS = 6000  # сколько слов оригинала отправлять в одном запросе при сжатии
CHUNK_MAX_PAGES = 30
SCAN_WORDS_PER_PAGE = 300  # оценка для сканов, где текст не извлекается
INLINE_LIMIT = 18 * 1024 * 1024
RETRY_DELAYS = (15, 45, 90)
RETRY_CODES = (408, 429, 500, 502, 503, 504)
GEMINI_TIMEOUT_MS = 10 * 60 * 1000  # зависший запрос не должен держать очередь вечно
MAX_NAME_BYTES = 180  # файловые системы допускают 255 байт на имя; оставляем место для суффиксов

Progress = Callable[[str], None]


class SummaryError(Exception):
    pass


class DailyLimitReached(SummaryError):
    """Дневной лимит запросов к Gemini исчерпан — задача вернётся в очередь."""


class NotEnoughToday(DailyLimitReached):
    """Остатка дневного лимита не хватит на всю книгу: отложить без трат и взять следующую."""


@dataclass
class Summary:
    fmt: str
    book: str  # «Название - Автор» — общее имя для всех форматов книги (безопасное для имени файла)
    markdown: str
    extra: dict | None = None  # дополнительные поля во frontmatter
    shown: str = ""  # то же без чистки для файловой системы («C#: Том 1») — для frontmatter

    @property
    def title(self) -> str:
        name = self.shown or self.book
        return name.rsplit(" - ", 1)[0].strip() or name

    @property
    def author(self) -> str:
        parts = (self.shown or self.book).rsplit(" - ", 1)
        return parts[1].strip() if len(parts) == 2 else "Неизвестно"


class Summarizer(Protocol):
    def run(self, kind: str, source: Path | str, formats: list[str], book: str | None,
            progress: Progress) -> Iterator[Summary]: ...


_BAD_CHARS = re.compile(r'[\\/*?:"<>|\x00-\x1f#^\[\]]')
# Невидимые и управляющие символы: направление текста, нулевой ширины, DEL и C1, разделители строк.
_BIDI = re.compile("[\u061c\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\x7f-\x9f\u2028\u2029]")
UNKNOWN = "Неизвестно"


def clean_text(value: str) -> str:
    """Для свойств заметки: без управляющих и невидимых символов, но со всеми знаками («C#: Том 1»)."""
    value = unicodedata.normalize("NFC", value)
    value = re.sub("[\x00-\x1f\x85\u2028\u2029]", " ", value)  # переводы строк — в пробелы
    value = _BIDI.sub("", value)
    return re.sub(r"\s+", " ", value).strip()


def safe_filename(name: str, fallback: str = "Без названия", limit: int = 100) -> str:
    """Имя файла, безопасное для диска и для Obsidian (без / \\ : # [ ] и т.п.).

    Буквы приводятся к одной форме (NFC: имя с Mac и с Windows совпадёт), управляющие символы
    направления текста убираются, длина ограничена и в символах, и в байтах."""
    name = unicodedata.normalize("NFC", name)
    cleaned = _BIDI.sub("", _BAD_CHARS.sub("", name)).strip().strip(".").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)[:limit].strip()
    while len(cleaned.encode()) > MAX_NAME_BYTES:
        cleaned = cleaned[:-1]
    cleaned = cleaned.strip().strip(".").strip()
    return cleaned or fallback


def book_name(raw: str, fallback: str = "Без названия") -> str:
    """«Название - Автор» для имени файла: название и автор укорачиваются по отдельности,
    чтобы длинное название не съело автора."""
    raw = clean_text(raw)
    title, sep, author = raw.rpartition(" - ")
    if not sep or not title.strip():
        return safe_filename(raw, fallback)
    title = safe_filename(title, fallback)
    author = safe_filename(author, UNKNOWN, limit=60)
    return f"{title} - {author}"


def _known(raw: str, fallback: str) -> str:
    """Если модель не узнала ни названия, ни автора, берём имя из метаданных или файла."""
    title, _, author = clean_text(raw).rpartition(" - ")
    if title.strip() in ("", UNKNOWN) and author.strip() in ("", UNKNOWN):
        return fallback
    return raw


def parse_output_full(text: str, fallback: str) -> tuple[str, str, str]:
    """(имя для файла, имя как есть для свойств, остальной текст)."""
    text = text.strip()
    # Иногда модель всё же оборачивает ответ в ```markdown ... ```
    fence = re.fullmatch(r"```[a-zA-Z]*\n(.*)\n```", text, flags=re.S)
    if fence:
        text = fence.group(1).strip()
    # Только «\n»: splitlines() резал бы и по U+2028/NEL внутри строки FILENAME.
    lines = text.replace("\r\n", "\n").split("\n")
    first = lines[0].strip().strip("*").strip()
    if first.upper().startswith("FILENAME:"):
        raw = _known(first.split(":", 1)[1].strip().strip("*"), fallback)
        return book_name(raw, safe_filename(fallback)), clean_text(raw), "\n".join(lines[1:]).strip() + "\n"
    return book_name(fallback), clean_text(fallback), text + "\n"


def parse_output(text: str, fallback: str) -> tuple[str, str]:
    """Возвращает (имя книги из строки FILENAME, остальной текст)."""
    name, _, body = parse_output_full(text, fallback)
    return name, body


def _yaml(value) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(value) + "]"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def with_frontmatter(markdown: str, meta: dict) -> str:
    """Добавляет поля во frontmatter заметки; уже существующие ключи не трогает."""
    body, front = markdown, ""
    if markdown.startswith("---\n"):
        end = markdown.find("\n---", 4)
        if end != -1:
            front, body = markdown[4:end], markdown[end + 4:].lstrip("-").lstrip("\n")
    have = set(re.findall(r"^([\w-]+):", front, flags=re.M))
    extra = "\n".join(f"{k}: {_yaml(v)}" for k, v in meta.items() if k not in have and v not in (None, ""))
    front = "\n".join(x for x in (front.strip("\n"), extra) if x)
    return f"---\n{front}\n---\n\n{body.lstrip()}"


def _pieces(para: str, chunk_words: int) -> list[str]:
    """Слишком длинный абзац делится по предложениям, а если и они длинные — по словам."""
    if len(para.split()) <= chunk_words:
        return [para]
    out = []
    for sentence in re.split(r"(?<=[.!?…])\s+|\n", para):
        words = sentence.split()
        for i in range(0, len(words), chunk_words):
            out.append(" ".join(words[i:i + chunk_words]))
    return out


def split_text(text: str, chunk_words: int = CHUNK_WORDS) -> list[str]:
    """Режет текст по абзацам на куски примерно по chunk_words слов."""
    chunks, cur, n = [], [], 0
    paragraphs = [piece for para in re.split(r"\n\s*\n", text) for piece in _pieces(para, chunk_words)]
    for para in paragraphs:
        w = len(para.split())
        if cur and n + w > chunk_words:
            chunks.append("\n\n".join(cur))
            cur, n = [], 0
        cur.append(para)
        n += w
    if cur:
        chunks.append("\n\n".join(cur))
    return [c for c in chunks if c.strip()]


def page_groups(words: list[int], chunk_words: int = CHUNK_WORDS, max_pages: int = CHUNK_MAX_PAGES) -> list[tuple[int, int]]:
    """Группирует страницы [a, b) так, чтобы в группе было ~chunk_words слов."""
    groups, start, n = [], 0, 0
    for i, w in enumerate(words):
        if i > start and (n + w > chunk_words or i - start >= max_pages):
            groups.append((start, i))
            start, n = i, 0
        n += w
    if words:
        groups.append((start, len(words)))
    return groups


@dataclass
class PdfLayout:
    """Разбиение PDF на части для сжатия: считается один раз, без запросов к Gemini."""
    reader: object
    words: list[int]
    groups: list[tuple[int, int]]


class GeminiSummarizer:
    def __init__(self, api_key: str, model: str, prompts_dir: Path | None = None,
                 client_factory: Callable | None = None, sleep: Callable[[float], None] = time.sleep,
                 max_requests: int = 60, meter: Callable[[], None] | None = None):
        if client_factory is None:
            from google import genai
            from google.genai import types

            self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS))
        else:
            self.client = client_factory(api_key=api_key)
        self.model = model
        self.prompts_dir = prompts_dir
        self.sleep = sleep
        self.max_requests = max_requests  # потолок запросов на одну книгу (0 — без потолка)
        self.meter = meter  # вызывается перед каждым запросом; бросает DailyLimitReached
        self.plan_check: Callable[[int], None] | None = None  # проверка плана до первого запроса
        # Готовые части сжатия: если книга упадёт на середине, «Повторить» не оплачивает их заново.
        self.parts_dir: Path | None = None

    # ----- промпты -----
    def prompt(self, fmt: str) -> str:
        """Свой промпт можно положить в data/prompts/<формат>.md (для конспекта — ещё и data/prompt.md)."""
        if self.prompts_dir:
            for p in (self.prompts_dir / "prompts" / f"{fmt}.md", self.prompts_dir / "prompt.md" if fmt == "summary" else None):
                if p and p.is_file():
                    return p.read_text("utf-8")
        return HEADER + "\n" + FORMATS[fmt].prompt

    # ----- вызовы Gemini -----
    def _generate(self, contents: list) -> str:
        if self.meter:
            self.meter()
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                res = self.client.models.generate_content(model=self.model, contents=contents)
                break
            except Exception as exc:
                code = getattr(exc, "code", None)
                if (code in RETRY_CODES or is_network_error(exc)) and attempt < len(RETRY_DELAYS):
                    log.warning("Gemini: %s, повтор через %s с", code or exc.__class__.__name__, RETRY_DELAYS[attempt])
                    self.sleep(RETRY_DELAYS[attempt])
                    continue
                raise
        text = getattr(res, "text", None)
        reason = ""
        try:
            reason = str(res.candidates[0].finish_reason or "")
        except Exception:
            pass
        if "MAX_TOKENS" in reason:
            raise SummaryError("Ответ модели обрезан по длине (MAX_TOKENS) — заметка была бы неполной")
        if not text:
            raise SummaryError(f"Модель вернула пустой ответ {reason}".strip())
        return text

    def _upload(self, file, display: str):
        up = self.client.files.upload(file=file, config={"mime_type": "application/pdf"})
        deadline = time.monotonic() + UPLOAD_TIMEOUT_S
        while _state(up) == "PROCESSING":
            if time.monotonic() > deadline:
                self._forget(up)
                raise SummaryError(f"Gemini слишком долго обрабатывает {display}")
            self.sleep(3)
            up = self.client.files.get(name=up.name)
        if _state(up) == "FAILED":
            self._forget(up)
            raise SummaryError(f"Gemini не смог прочитать {display}")
        return up

    def _forget(self, up) -> None:
        try:
            self.client.files.delete(name=up.name)
        except Exception:
            log.warning("Не удалось удалить файл %s из Gemini", getattr(up, "name", "?"))

    # ----- основной вход -----
    def run(self, kind: str, source: Path | str, formats: list[str], book: str | None,
            progress: Progress) -> Iterator[Summary]:
        """kind: "url" — ссылка; "file" (или старое "pdf") — путь к файлу книги."""
        whole = [f for f in formats if not FORMATS[f].ratio]
        squeeze = [f for f in formats if FORMATS[f].ratio]
        is_pdf = kind != "url" and Path(source).suffix.lower() == ".pdf"
        material: object = None
        text = ""
        shown = ""

        if kind == "url":
            progress("скачиваю страницу")
            text, fallback = fetch_article(str(source))
            material = f"Источник: {source}\nЗаголовок страницы: {fallback}\n\n{text}"
        elif is_pdf:
            fallback = split_name(Path(source).name)[0]
        else:
            progress("читаю файл")
            book_file = extract_text(Path(source))
            text = book_file.text
            fallback = (f"{book_file.title} - {book_file.author}" if book_file.title and book_file.author
                        else book_file.title or split_name(Path(source).name)[0])
            if book_file.title and book_file.author and not book:
                # Метаданные точнее догадки модели — и сжатие (оно видит только кусок текста) их не теряет.
                book = book_name(fallback)
                shown = clean_text(fallback)
            material = "\n".join(x for x in (f"Книга (файл {Path(source).name}).", book_file.hint) if x)
            material += "\n\n" + text[:MAX_BOOK_TEXT]

        # Сначала считаем, сколько запросов понадобится, и только потом тратим деньги.
        layout = self._pdf_layout(Path(source), progress) if is_pdf and squeeze else None
        parts = len(layout.groups) if layout else (len(split_text(text)) if squeeze else 0)
        planned = len(whole) + parts * len(squeeze)
        if self.max_requests and planned > self.max_requests:
            raise SummaryError(
                f"Для этой книги нужно {planned} {requests_word(planned)} к Gemini — больше лимита {self.max_requests} "
                "на одну книгу (MAX_REQUESTS_PER_JOB). Уберите сжатие или увеличьте лимит."
            )
        if self.plan_check:
            self.plan_check(planned)  # хватит ли сегодняшнего лимита на всю книгу

        if whole:
            up = None
            if is_pdf:
                progress("загружаю PDF в Gemini")
                up = self._upload(str(source), "этот PDF")
                material = up
            try:
                for fmt in whole:
                    progress(FORMATS[fmt].label)
                    name, raw, body = parse_output_full(self._generate([material, self.prompt(fmt)]), fallback)
                    if not book:
                        book, shown = name, raw
                    yield Summary(fmt, book, body, shown=shown)
            finally:
                if up is not None:
                    self._forget(up)

        for fmt in squeeze:
            if layout:
                summary = self._compress_pdf(layout, fmt, book, fallback, progress)
            else:
                summary = self._compress_text(text, fmt, book, fallback, progress)
            summary.shown = summary.shown or shown
            book, shown = summary.book, summary.shown
            yield summary

    # ----- сжатие частями -----
    def _compress(self, parts: list[tuple[object, int]], fmt: str, book: str | None, fallback: str,
                  progress: Progress) -> Summary:
        ratio = FORMATS[fmt].ratio
        out, shown, saved = [], "", []
        for i, (content, words) in enumerate(parts):
            progress(f"{FORMATS[fmt].label}: часть {i + 1} из {len(parts)}")
            target = max(60, round(words / ratio))
            prompt = compress_prompt(ratio, target, i + 1, len(parts), i == 0, book)
            cached = self._part_path(content, prompt)
            if cached and cached.is_file():
                text = cached.read_text("utf-8")
                saved.append(cached)
                if i == 0 and not book:
                    book, shown, text = parse_output_full(text, fallback)
                out.append(text.strip())
                continue
            uploaded = None
            if isinstance(content, bytes):
                if len(content) <= INLINE_LIMIT:
                    from google.genai import types

                    # Официальный конструктор: SDK сам сериализует поля как inlineData / mimeType.
                    content = types.Part.from_bytes(data=content, mime_type="application/pdf")
                else:
                    content = uploaded = self._upload(io.BytesIO(content), f"часть {i + 1}")
            try:
                text = self._generate([content, prompt])
            finally:
                if uploaded is not None:
                    self._forget(uploaded)
            if cached:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(text, "utf-8")
                saved.append(cached)
            if i == 0 and not book:
                book, shown, text = parse_output_full(text, fallback)
            out.append(text.strip())
        for path in saved:  # формат готов — сохранённые части больше не нужны
            path.unlink(missing_ok=True)
        source_words = sum(w for _, w in parts)
        result_words = sum(len(t.split()) for t in out)
        intro = (f"> [!info] {FORMATS[fmt].label}\n> Оригинал ≈ {source_words:,} слов → пересказ ≈ {result_words:,} слов"
                 .replace(",", " "))
        return Summary(fmt, book or book_name(fallback), intro + "\n\n" + "\n\n".join(out) + "\n",
                       {"source_words": source_words, "words": result_words}, shown=shown)

    def _part_path(self, content: object, prompt: str) -> Path | None:
        if not self.parts_dir:
            return None
        h = hashlib.sha256(self.model.encode() + b"\0" + prompt.encode())
        h.update(content if isinstance(content, bytes) else str(content).encode())
        return self.parts_dir / f"{h.hexdigest()[:32]}.txt"

    def _pdf_layout(self, path: Path, progress: Progress) -> PdfLayout:
        from pypdf import PdfReader

        progress("читаю PDF")
        reader = PdfReader(path)
        words = []
        for page in reader.pages:
            try:
                words.append(len((page.extract_text() or "").split()))
            except Exception:
                words.append(0)
        if sum(words) < 50 * max(1, len(words)):  # скан без текстового слоя
            words = [SCAN_WORDS_PER_PAGE] * len(words)
        groups = page_groups(words)
        if not groups:
            raise SummaryError("В PDF нет страниц")
        return PdfLayout(reader, words, groups)

    def _compress_pdf(self, layout: PdfLayout, fmt: str, book: str | None, fallback: str,
                      progress: Progress) -> Summary:
        from pypdf import PdfWriter

        parts = []
        for a, b in layout.groups:
            writer = PdfWriter()
            for page in layout.reader.pages[a:b]:
                writer.add_page(page)
            buf = io.BytesIO()
            writer.write(buf)
            parts.append((buf.getvalue(), sum(layout.words[a:b])))
        return self._compress(parts, fmt, book, fallback, progress)

    def _compress_text(self, text: str, fmt: str, book: str | None, fallback: str, progress: Progress) -> Summary:
        parts = [(chunk, len(chunk.split())) for chunk in split_text(text)]
        return self._compress(parts, fmt, book, fallback, progress)


def requests_word(n: int) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return "запросов"
    return {1: "запрос", 2: "запроса", 3: "запроса", 4: "запроса"}.get(n % 10, "запросов")


def is_network_error(exc: BaseException) -> bool:
    """Обрыв связи или таймаут (httpx/httpcore, сокеты): такой запрос стоит повторить."""
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    for cls in type(exc).__mro__:
        if cls.__module__.split(".")[0] in ("httpx", "httpcore") and cls.__name__ in (
                "TransportError", "TimeoutException", "NetworkError", "RemoteProtocolError",
                "ConnectError", "ReadError", "WriteError", "ReadTimeout", "ConnectTimeout", "PoolTimeout"):
            return True
    return False


def fetch_article(url: str) -> tuple[str, str]:
    import trafilatura

    html = trafilatura.fetch_url(url)
    if not html:
        raise SummaryError("Не удалось скачать страницу: сайт недоступен, требует входа или не пускает "
                           "программы — сохраните статью как HTML и загрузите файлом")
    text = trafilatura.extract(html) or ""
    if len(text) < 200:
        raise SummaryError("Не удалось извлечь текст со страницы (меньше 200 символов)")
    meta = trafilatura.extract_metadata(html)
    title = (meta.title if meta and meta.title else "") or "Статья"
    return text[:MAX_URL_TEXT], title


def _state(file) -> str:
    state = getattr(file, "state", None)
    return getattr(state, "name", str(state or ""))

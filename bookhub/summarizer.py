"""Конспекты через Gemini: PDF целиком или текст статьи по ссылке."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

DEFAULT_PROMPT = """Ты — аналитик книг и статей. Сделай глубокий структурированный конспект на русском языке для заметки в Obsidian (в стиле NotebookLM).

Формат ответа — строго:
1. Первая строка: FILENAME: <Название> - <Автор>
2. Дальше — Markdown-заметка такой структуры:

---
title: "<Название>"
author: "<Автор>"
tags: [book-summary]
status: unread
created: {date}
---

> [!abstract] Главная идея
> Суть материала в 3–4 предложениях.

## Ключевые идеи и ментальные модели
## Разбор по главам (или разделам)
## Сильные цитаты
Только дословные цитаты из текста, ничего не выдумывай.
## Практический план
- [ ] Шаг 1
## Кому и зачем читать
## Связанные темы
[[Тема 1]], [[Тема 2]], [[Тема 3]]

Опирайся только на предоставленный материал. Если название или автор неизвестны, пиши «Неизвестно». Не оборачивай ответ в блок кода."""

MAX_URL_TEXT = 400_000
UPLOAD_TIMEOUT_S = 600


class SummaryError(Exception):
    pass


@dataclass
class Summary:
    title: str
    markdown: str


class Summarizer(Protocol):
    def summarize_pdf(self, path: Path) -> Summary: ...
    def summarize_url(self, url: str) -> Summary: ...


_BAD_CHARS = re.compile(r'[\\/*?:"<>|\x00-\x1f#^\[\]]')


def safe_filename(name: str, fallback: str = "Без названия", limit: int = 100) -> str:
    """Имя файла, безопасное для диска и для Obsidian (без / \\ : # [ ] и т.п.)."""
    cleaned = _BAD_CHARS.sub("", name).strip().strip(".").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)[:limit].strip()
    return cleaned or fallback


def parse_output(text: str, fallback: str) -> Summary:
    text = text.strip()
    # Иногда модель всё же оборачивает ответ в ```markdown ... ```
    fence = re.fullmatch(r"```[a-zA-Z]*\n(.*)\n```", text, flags=re.S)
    if fence:
        text = fence.group(1).strip()
    lines = text.splitlines()
    if lines:
        first = lines[0].strip().strip("*").strip()
        if first.upper().startswith("FILENAME:"):
            title = safe_filename(first.split(":", 1)[1].strip().strip("*"), fallback)
            return Summary(title, "\n".join(lines[1:]).strip() + "\n")
    return Summary(safe_filename(fallback), text + "\n")


def add_source(markdown: str, source: str) -> str:
    """Добавляет поле source во frontmatter заметки (если его там ещё нет)."""
    if not markdown.startswith("---\n"):
        return f"---\nsource: {json.dumps(source, ensure_ascii=False)}\n---\n\n{markdown}"
    end = markdown.find("\n---", 4)
    if end == -1:
        return markdown
    front = markdown[4:end]
    if re.search(r"^source:", front, flags=re.M):
        return markdown
    return f"---\n{front}\nsource: {json.dumps(source, ensure_ascii=False)}{markdown[end:]}"


class GeminiSummarizer:
    def __init__(self, api_key: str, model: str, timezone: str, prompt_file: Path | None = None,
                 client_factory: Callable | None = None):
        if client_factory is None:
            from google import genai

            client_factory = genai.Client
        self.client = client_factory(api_key=api_key)
        self.model = model
        self.tz = ZoneInfo(timezone)
        self.prompt_file = prompt_file

    def prompt(self) -> str:
        template = DEFAULT_PROMPT
        if self.prompt_file and self.prompt_file.is_file():
            template = self.prompt_file.read_text("utf-8")
        return template.replace("{date}", datetime.now(self.tz).strftime("%Y-%m-%d"))

    def _generate(self, contents: list) -> str:
        res = self.client.models.generate_content(model=self.model, contents=contents)
        text = getattr(res, "text", None)
        if not text:
            reason = ""
            try:
                reason = str(res.candidates[0].finish_reason)
            except Exception:
                pass
            raise SummaryError(f"Модель вернула пустой ответ {reason}".strip())
        return text

    def summarize_pdf(self, path: Path) -> Summary:
        up = self.client.files.upload(file=str(path), config={"mime_type": "application/pdf"})
        try:
            deadline = time.monotonic() + UPLOAD_TIMEOUT_S
            while _state(up) == "PROCESSING":
                if time.monotonic() > deadline:
                    raise SummaryError("Gemini слишком долго обрабатывает PDF")
                time.sleep(3)
                up = self.client.files.get(name=up.name)
            if _state(up) == "FAILED":
                raise SummaryError("Gemini не смог прочитать этот PDF")
            return parse_output(self._generate([up, self.prompt()]), path.stem)
        finally:
            try:
                self.client.files.delete(name=up.name)
            except Exception:
                log.warning("Не удалось удалить файл %s из Gemini", getattr(up, "name", "?"))

    def summarize_url(self, url: str) -> Summary:
        import trafilatura

        html = trafilatura.fetch_url(url)
        if not html:
            raise SummaryError("Не удалось скачать страницу")
        text = trafilatura.extract(html) or ""
        if len(text) < 200:
            raise SummaryError("Не удалось извлечь текст со страницы (меньше 200 символов)")
        meta = trafilatura.extract_metadata(html)
        page_title = (meta.title if meta and meta.title else "") or "Статья"
        material = f"Источник: {url}\nЗаголовок страницы: {page_title}\n\n{text[:MAX_URL_TEXT]}"
        return parse_output(self._generate([material, self.prompt()]), page_title)


def _state(file) -> str:
    state = getattr(file, "state", None)
    return getattr(state, "name", str(state or ""))

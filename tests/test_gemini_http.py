"""Настоящий SDK google-genai против локального поддельного сервера Gemini.

Проверяет то, чего не видно с заглушкой на уровне Python: загрузку PDF по протоколу
resumable upload, формат запросов, обработку ответа 429 и таймаут."""

import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from google import genai
from google.genai import types
from pypdf import PdfWriter

from bookhub.summarizer import GeminiSummarizer


class FakeGemini:
    def __init__(self):
        self.calls = []
        self.uploaded = b""
        self.deleted = []
        self.fail_left = 0
        self.delay = 0.0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, code, payload, headers=None):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def do_POST(self):
                data = self._body()
                if self.path.startswith("/upload/v1beta/files"):
                    port = self.server.server_address[1]
                    return self._json(200, {}, {"x-goog-upload-url": f"http://127.0.0.1:{port}/upload-session/1"})
                if self.path.startswith("/upload-session/"):
                    fake.uploaded += data
                    if "finalize" in self.headers.get("X-Goog-Upload-Command", ""):
                        return self._json(200, {"file": {"name": "files/f1", "mimeType": "application/pdf",
                                                         "state": "ACTIVE", "uri": "https://fake/files/f1"}},
                                          {"x-goog-upload-status": "final"})
                    return self._json(200, {}, {"x-goog-upload-status": "active"})
                if ":generateContent" in self.path:
                    if fake.delay:
                        time.sleep(fake.delay)
                    req = json.loads(data)
                    fake.calls.append(req)
                    if fake.fail_left:
                        fake.fail_left -= 1
                        return self._json(429, {"error": {"code": 429, "message": "Resource has been exhausted",
                                                          "status": "RESOURCE_EXHAUSTED"}})
                    prompt = json.dumps(req, ensure_ascii=False)
                    if "Перед тобой часть" in prompt:
                        text = ("FILENAME: Книга - Автор\n" if "FILENAME" in prompt else "") + "Сжатый пересказ части."
                    else:
                        text = "FILENAME: Мастер и Маргарита - Михаил Булгаков\n## Раздел\nТекст заметки."
                    return self._json(200, {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]},
                                                            "finishReason": "STOP"}]})
                self._json(404, {"error": {"code": 404, "message": "not found"}})

            def do_GET(self):
                if self.path.startswith("/v1beta/files/"):
                    return self._json(200, {"name": "files/f1", "state": "ACTIVE"})
                self._json(404, {})

            def do_DELETE(self):
                fake.deleted.append(self.path)
                self._json(200, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def summarizer(self, tmp_path, timeout_ms=5000, sleep=lambda s: None):
        def factory(api_key):
            return genai.Client(api_key=api_key, http_options=types.HttpOptions(
                base_url=f"http://127.0.0.1:{self.port}/", timeout=timeout_ms))
        return GeminiSummarizer("test-key", "gemini-test", tmp_path, client_factory=factory, sleep=sleep)


@pytest.fixture
def gemini():
    fake = FakeGemini()
    yield fake
    fake.server.shutdown()


def blank_pdf(path, pages):
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    path.write_bytes(buf.getvalue())


def test_pdf_upload_formats_and_compression_through_real_sdk(tmp_path, gemini):
    pdf = tmp_path / "book.pdf"
    blank_pdf(pdf, 5)
    g = gemini.summarizer(tmp_path)
    out = list(g.run("file", pdf, ["summary", "quotes", "x2"], None, lambda t: None))
    assert [o.fmt for o in out] == ["summary", "quotes", "x2"]
    assert out[0].book == "Мастер и Маргарита - Михаил Булгаков" and "## Раздел" in out[0].markdown
    assert gemini.uploaded == pdf.read_bytes()  # PDF загружен целиком, по протоколу resumable upload
    assert gemini.deleted == ["/v1beta/files/f1"]  # и удалён из Gemini после разбора
    assert len(gemini.calls) == 3
    whole = json.dumps(gemini.calls[0])
    assert "https://fake/files/f1" in whole and "Подробный конспект" not in whole  # ссылка на файл + промпт
    inline = gemini.calls[2]["contents"][0]["parts"][0]["inlineData"]
    # часть для сжатия уходит прямо в запросе (SDK пишет вложенные поля как mime_type — API принимает оба вида)
    assert (inline.get("mimeType") or inline.get("mime_type")) == "application/pdf" and inline["data"].startswith("JVBER")


def test_rate_limit_429_is_retried(tmp_path, gemini):
    gemini.fail_left = 2
    slept = []
    g = gemini.summarizer(tmp_path, sleep=slept.append)
    text = tmp_path / "a.txt"
    text.write_text("Глава первая.\n\n" + "Длинный текст книги. " * 40, "utf-8")
    out = list(g.run("file", text, ["key"], None, lambda t: None))
    assert slept == [15, 45] and out[0].fmt == "key" and len(gemini.calls) == 3


def test_hanging_request_times_out(tmp_path, gemini):
    gemini.delay = 2.0
    g = gemini.summarizer(tmp_path, timeout_ms=300)
    text = tmp_path / "a.txt"
    text.write_text("Текст книги. " * 50, "utf-8")
    started = time.monotonic()
    with pytest.raises(httpx.TimeoutException):
        list(g.run("file", text, ["brief"], None, lambda t: None))
    assert time.monotonic() - started < 1.9  # не ждём ответа вечно


def test_daily_meter_is_called_per_request(tmp_path, gemini):
    from bookhub.summarizer import DailyLimitReached
    g = gemini.summarizer(tmp_path)
    left = [1]

    def meter():
        if not left[0]:
            raise DailyLimitReached("лимит")
        left[0] -= 1

    g.meter = meter
    text = tmp_path / "a.txt"
    text.write_text("Текст книги. " * 50, "utf-8")
    gen = g.run("file", text, ["summary", "quotes"], None, lambda t: None)
    assert next(gen).fmt == "summary"
    with pytest.raises(DailyLimitReached):
        next(gen)
    assert len(gemini.calls) == 1  # второй запрос даже не отправлен

import asyncio
import urllib.parse
import io
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from bookhub.config import ConfigError, Settings
from bookhub.extract import ExtractError, extract_text, looks_valid, split_name
from bookhub.formats import parse_formats
from bookhub.library import read_meta
from bookhub.main import create_app, render_note
from bookhub.store import JobStore
from bookhub.summarizer import (GeminiSummarizer, Summary, page_groups, parse_output, safe_filename,
                                split_text, with_frontmatter)
from bookhub.worker import unique_path

AUTH = ("me", "secret-pass-42")
PDF = b"%PDF-1.4\n%fake\n"


class FakeSummarizer:
    """Заглушка: для PDF книга «<имя файла> - Автор», для ссылок — «Статья - Кто-то»."""

    def __init__(self, fail=False, fail_on=None):
        self.fail = fail
        self.fail_on = fail_on
        self.calls = []

    def run(self, kind, source, formats, book, progress):
        for fmt in formats:
            self.calls.append((kind, Path(str(source)).name, fmt, book))
            progress(fmt)
            if self.fail or fmt == self.fail_on:
                raise RuntimeError("модель недоступна")
            if kind != "url":
                name = book or f"{split_name(Path(source).name)[0]} - Автор"
                body = "> [!abstract] Идея\n> Текст\n\n- [ ] шаг\n"
            else:
                name = book or "Статья - Кто-то"
                body = "# Статья\n<script>alert(1)</script>\n[[Тема]]\nВопрос::Ответ\n"
            yield Summary(fmt, name, body)


def make(tmp_path, summarizer=None, **kw):
    s = Settings(web_password="secret-pass-42", data_dir=tmp_path / "data", **kw)
    app = create_app(s, summarizer=summarizer, start_scheduler=False)
    app.state.limiter.fail_delay = 0  # пауза после неверного пароля проверяется отдельным тестом
    return TestClient(app), s, app


def upload(client, name="Моя книга.pdf", data=None, formats=(), **kw):
    return client.post("/add", auth=AUTH, files=[("files", (name, data or PDF, "application/pdf"))],
                       data={"formats": list(formats)}, follow_redirects=False, **kw)


def run_queue(app, limit=None):
    return asyncio.run(app.state.processor.run(limit))


# ---------- сайт ----------

def test_password_required(tmp_path):
    with pytest.raises(ConfigError):
        create_app(Settings(web_password="", data_dir=tmp_path), start_scheduler=False)


def test_auth(tmp_path):
    client, _, _ = make(tmp_path)
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("me", "wrong")).status_code == 401
    assert client.get("/", auth=("other", "secret-pass-42")).status_code == 401
    r = client.get("/", auth=AUTH)
    assert r.status_code == 200 and "Book Hub" in r.text and "Сжатие ×3" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert client.get("/health").json() == {"ok": True, "db": True, "disk": True, "queue": True}


def test_upload_process_and_view(tmp_path):
    fake = FakeSummarizer()
    client, s, app = make(tmp_path, fake)
    r = upload(client)
    assert r.status_code == 303 and r.headers["location"] == "/?m=added"
    assert (s.inbox / "Моя книга.pdf").exists()
    assert "Моя книга" in client.get("/", auth=AUTH).text

    assert run_queue(app) == 1
    note = s.books_dir / "Моя книга - Автор.md"
    meta = read_meta(note)
    assert meta["source"] == "Моя книга.pdf" and meta["format"] == "summary"
    assert meta["title"] == "Моя книга" and meta["author"] == "Автор"
    assert not (s.inbox / "Моя книга.pdf").exists()
    assert (s.archive / "Моя книга.pdf").exists()

    page = client.get("/notes/Моя книга - Автор.md", auth=AUTH)
    assert page.status_code == 200 and "obsidian://new?" in page.text and "☐" in page.text
    assert "Сделать из этой книги ещё" in page.text
    dl = client.get("/notes/Моя книга - Автор.md/download", auth=AUTH)
    assert dl.status_code == 200 and dl.content.startswith(b"---")

    # Повторная загрузка файла с тем же именем не теряется
    upload(client)
    assert (s.inbox / "Моя книга (2).pdf").exists()
    assert run_queue(app) == 1
    assert len(list(s.books_dir.glob("*.md"))) == 2


def test_several_formats_make_several_notes(tmp_path):
    fake = FakeSummarizer()
    client, s, app = make(tmp_path, fake)
    upload(client, "Б.pdf", formats=["x3", "quotes", "bogus", "characters"])
    job = app.state.store.list()[0]
    assert job.format_list == ["quotes", "characters", "x3"]  # порядок как в списке форматов, мусор отброшен
    assert run_queue(app) == 3
    names = sorted(p.name for p in s.books_dir.glob("*.md"))
    assert names == ["Б - Автор (герои).md", "Б - Автор (сжатие x3).md", "Б - Автор (цитаты).md"]
    home = client.get("/", auth=AUTH).text
    assert home.count('class="book-card') == 1  # одна книга в библиотеке, три формата
    assert "Лучшие цитаты по темам" in home and "Герои (ГГ и персонажи)" in home


def test_more_formats_from_note_page(tmp_path):
    fake = FakeSummarizer()
    client, s, app = make(tmp_path, fake)
    upload(client, "Книга.pdf")
    run_queue(app)
    r = client.post("/notes/Книга - Автор.md/more", auth=AUTH, data={"formats": ["summary", "x5", "key"]},
                    follow_redirects=False)
    assert r.headers["location"] == "/?m=more"
    job = app.state.store.list("queued")[0]
    assert job.format_list == ["key", "x5"] and job.book == "Книга - Автор"  # уже готовый конспект не повторяем
    assert run_queue(app) == 2
    assert fake.calls[-1] == ("file", "Книга.pdf", "x5", "Книга - Автор")  # PDF взят из архива
    assert (s.books_dir / "Книга - Автор (сжатие x5).md").exists()
    page = client.get("/notes/Книга - Автор (сжатие x5).md", auth=AUTH).text
    assert 'class="tag current"' in page and 'aria-current="page"' in page and "Ключевые моменты" in page


def test_more_formats_without_source(tmp_path):
    client, s, app = make(tmp_path, FakeSummarizer())
    (s.books_dir / "Старая.md").write_text("---\ntitle: x\n---\nтекст", "utf-8")
    r = client.post("/notes/Старая.md/more", auth=AUTH, data={"formats": ["key"]}, follow_redirects=False)
    assert r.headers["location"] == "/?m=nosource"


def test_retry_skips_finished_formats(tmp_path):
    fake = FakeSummarizer(fail_on="x2")
    client, s, app = make(tmp_path, fake)
    upload(client, "a.pdf", formats=["summary", "x2"])
    assert run_queue(app) == 1
    job = app.state.store.list()[0]
    assert job.status == "error" and job.done_list == ["summary"] and "готово: 1" in job.error
    assert "недоступна" in client.get("/", auth=AUTH).text
    fake.fail_on = None
    client.post(f"/jobs/{job.id}/retry", auth=AUTH)
    assert run_queue(app) == 1
    assert [c[2] for c in fake.calls] == ["summary", "x2", "x2"]
    assert fake.calls[-1][3] == "a - Автор"  # имя книги сохранилось между попытками
    assert app.state.store.get(job.id).status == "done"


def test_upload_rejects_non_pdf(tmp_path):
    client, s, _ = make(tmp_path, FakeSummarizer(), max_upload_mb=1)
    assert upload(client, "x.pdf", b"not a pdf").headers["location"] == "/?m=rejected"
    upload(client, "big.pdf", PDF + b"0" * (2 * 1024 * 1024))
    upload(client, "x.exe")
    assert list(s.inbox.iterdir()) == []
    r = client.post("/add", auth=AUTH, data={"url": ""}, follow_redirects=False)
    assert r.headers["location"] == "/?m=nothing"


def test_upload_filename_cannot_escape_inbox(tmp_path):
    client, s, _ = make(tmp_path, FakeSummarizer())
    upload(client, "../../evil.pdf")
    assert not (tmp_path / "evil.pdf").exists()
    assert [p.name for p in s.inbox.iterdir()] == ["evil.pdf"]


def test_note_path_traversal(tmp_path):
    client, s, _ = make(tmp_path)
    (s.data_dir / "secret.md").write_text("x")
    for name in ["..%2F..%2Fsecret.md", "..%2Fsecret.md", "nope.md", "bookhub.db"]:
        assert client.get(f"/notes/{name}", auth=AUTH).status_code == 404


def test_url_job_and_html_is_escaped(tmp_path):
    client, s, app = make(tmp_path, FakeSummarizer())
    r = client.post("/add", auth=AUTH, data={"url": "javascript:alert(1)"}, follow_redirects=False)
    assert r.headers["location"] == "/?m=badurl"
    client.post("/add", auth=AUTH, data={"url": "https://example.com/a", "formats": ["cards"]})
    assert run_queue(app) == 1
    page = client.get("/notes/Статья - Кто-то (карточки).md", auth=AUTH).text
    assert "<script>alert(1)" not in page
    assert '<span class="wikilink">Тема</span>' in page
    assert "<strong>Вопрос</strong> — Ответ" in page
    assert read_meta(s.books_dir / "Статья - Кто-то (карточки).md")["source"] == "https://example.com/a"


def test_delete_queued_pdf_removes_file(tmp_path):
    client, s, app = make(tmp_path, FakeSummarizer())
    upload(client, "a.pdf")
    job = app.state.store.list()[0]
    client.post(f"/jobs/{job.id}/delete", auth=AUTH)
    assert app.state.store.list() == [] and list(s.inbox.iterdir()) == []
    client.get("/", auth=AUTH)  # не должно вернуться через inbox
    assert app.state.store.list() == []


def test_inbox_folder_uses_default_formats(tmp_path):
    client, s, app = make(tmp_path, FakeSummarizer(), default_formats=("brief", "key"))
    (s.inbox / "dropped.PDF").write_bytes(PDF)
    assert run_queue(app) == 2
    assert (s.books_dir / "dropped - Автор (кратко).md").exists()
    assert 'value="brief" checked' in client.get("/", auth=AUTH).text


def test_cross_site_post_is_blocked(tmp_path):
    client, _, _ = make(tmp_path)
    r = client.post("/add", auth=AUTH, data={"url": "https://e.com"}, headers={"origin": "https://evil.com"})
    assert r.status_code == 403
    r = client.post("/add", auth=AUTH, data={"url": "https://e.com"}, headers={"origin": "http://testserver"},
                    follow_redirects=False)
    assert r.status_code == 303


def test_run_without_key(tmp_path):
    client, _, _ = make(tmp_path)
    r = client.post("/run", auth=AUTH, follow_redirects=False)
    assert r.headers["location"] == "/?m=nokey"


def test_old_database_is_migrated(tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as c:
        c.execute("""CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, source TEXT NOT NULL,
                     title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', error TEXT, note TEXT,
                     created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        c.execute("INSERT INTO jobs (kind, source, title, created_at, updated_at) VALUES ('pdf','a.pdf','a','t','t')")
    store = JobStore(db)
    job = store.list()[0]
    assert job.format_list == ["summary"] and job.done_list == [] and job.is_file and job.type_label == "PDF"
    store.add("file", "b.epub", "b")  # старое ограничение kind IN ('pdf','url') снято
    assert [j.type_label for j in store.list()] == ["PDF", "EPUB"]
    JobStore(db)  # повторный запуск не ломает уже обновлённую базу


# ---------- git ----------

def ls_remote(remote):
    return subprocess.run(["git", "-c", "core.quotepath=off", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                          capture_output=True, text=True, check=True).stdout


def test_git_sync_to_bare_repo(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    client, s, app = make(tmp_path, FakeSummarizer(), git_repo_url=str(remote))
    upload(client, "g.pdf")
    assert run_queue(app) == 1
    git = app.state.git
    assert git.last_error is None and git.last_sync
    assert "Books/g - Автор.md" in ls_remote(remote)


def test_git_sync_joins_existing_vault(tmp_path):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(seed)], check=True, capture_output=True)
    (seed / "Existing.md").write_text("old note")
    for cmd in (["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "seed"], ["push", "origin", "HEAD:main"]):
        subprocess.run(["git", *cmd], cwd=seed, check=True, capture_output=True)
    client, s, app = make(tmp_path, FakeSummarizer(), git_repo_url=str(remote))
    upload(client, "n.pdf")
    run_queue(app)
    assert app.state.git.last_error is None
    files = ls_remote(remote)
    assert "Existing.md" in files and "Books/n - Автор.md" in files


# ---------- разбор ответа и утилиты ----------

def test_parse_output():
    name, body = parse_output("**FILENAME: Атомные привычки - Джеймс Клир**\n## Идеи\nтекст", "fb")
    assert name == "Атомные привычки - Джеймс Клир" and body.startswith("## Идеи")
    assert parse_output("```markdown\nFILENAME: A/B: C\nbody\n```", "fb") == ("AB C", "body\n")
    assert parse_output("no header", "fallback")[0] == "fallback"


def test_summary_title_author():
    s = Summary("summary", "Война и мир - Лев Толстой", "")
    assert (s.title, s.author) == ("Война и мир", "Лев Толстой")
    assert Summary("summary", "Без автора", "").author == "Неизвестно"


def test_safe_filename():
    assert safe_filename("../a/b") == "ab"
    assert safe_filename('  "?*  ') == "Без названия"
    assert safe_filename("Книга #1 [часть]") == "Книга 1 часть"
    assert len(safe_filename("x" * 500)) == 100


def test_with_frontmatter():
    out = with_frontmatter("body", {"title": "a", "tags": ["book", "book/x2"], "words": 5, "empty": ""})
    assert out == '---\ntitle: "a"\ntags: [book, book/x2]\nwords: 5\n---\n\nbody'
    # ключи, которые модель уже написала сама, не дублируются
    out = with_frontmatter("---\ntitle: mine\n---\n\nbody", {"title": "a", "source": "f.pdf"})
    assert out == '---\ntitle: mine\nsource: "f.pdf"\n---\n\nbody'


def test_render_note_strips_frontmatter():
    front, html = render_note("---\ntitle: a\n---\n\n- first\n- [x] done\n")
    assert front == "title: a" and "<li>first</li>" in html and "☑" in html


def test_parse_formats():
    assert parse_formats(["x5", "summary", "x5", "nope"]) == ["summary", "x5"]
    assert parse_formats("", ["key"]) == ["key"]
    assert parse_formats("quotes, cards") == ["quotes", "cards"]


def test_chunking():
    assert page_groups([3000, 3000, 3000, 100]) == [(0, 2), (2, 4)]
    assert page_groups([10] * 70, max_pages=30) == [(0, 30), (30, 60), (60, 70)]
    assert page_groups([]) == []
    text = "\n\n".join(["слово " * 100] * 130)  # 13 000 слов
    parts = split_text(text, 6000)
    assert len(parts) == 3 and sum(len(p.split()) for p in parts) == 13000


def test_unique_path(tmp_path):
    (tmp_path / "A.md").write_text("x")
    assert unique_path(tmp_path, "A", ".md").name == "A (2).md"


# ---------- Gemini с поддельным клиентом ----------

class FakeClient:
    def __init__(self, api_key, fail_first=0):
        self.uploads, self.deleted, self.requests = [], [], []
        self.fail_left = fail_first
        client = self

        class Files:
            def upload(self, file, config):
                client.uploads.append(file)
                state = type("S", (), {"name": "ACTIVE"})()
                return type("F", (), {"name": f"files/{len(client.uploads)}", "state": state})()

            def delete(self, name):
                client.deleted.append(name)

        class Models:
            def generate_content(self, model, contents):
                if client.fail_left:
                    client.fail_left -= 1
                    err = RuntimeError("rate limit")
                    err.code = 429
                    raise err
                client.requests.append(contents)
                prompt = contents[-1]
                if "Перед тобой часть" in prompt:
                    head = "FILENAME: Книга - Автор\n" if "FILENAME" in prompt else ""
                    return type("R", (), {"text": head + "сжатый текст части"})()
                return type("R", (), {"text": "FILENAME: Книга - Автор\n## Раздел\nтекст"})()

        self.files, self.models = Files(), Models()


def blank_pdf(path, pages):
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    path.write_bytes(buf.getvalue())


def test_gemini_pdf_formats_and_compression(tmp_path):
    pdf = tmp_path / "b.pdf"
    blank_pdf(pdf, 45)  # скан без текста → 300 слов на страницу → 3 части по ≤20 страниц
    holder = {}

    def factory(api_key):
        holder["c"] = FakeClient(api_key)
        return holder["c"]

    g = GeminiSummarizer("k", "m", tmp_path, client_factory=factory, sleep=lambda s: None)
    steps = []
    out = list(g.run("pdf", pdf, ["summary", "quotes", "x3"], None, steps.append))
    c = holder["c"]
    assert [o.fmt for o in out] == ["summary", "quotes", "x3"]
    assert all(o.book == "Книга - Автор" for o in out)
    assert len(c.uploads) == 1 and c.deleted == ["files/1"]  # весь PDF загружен один раз на оба формата
    compress = [r for r in c.requests if getattr(r[0], "inline_data", None)]
    assert len(compress) == 3 and compress[0][0].inline_data.mime_type == "application/pdf"
    assert "до 2000 слов" in compress[0][1]  # 20 стр × 300 слов / 3
    assert "FILENAME" not in compress[0][1]  # имя книги уже известно из конспекта
    assert out[2].markdown.startswith("> [!info] Сжатие ×3")
    assert out[2].extra["source_words"] == 45 * 300
    assert "Сжатие ×3: часть 3 из 3" in steps


def test_gemini_compression_only_gets_name_from_first_part(tmp_path):
    pdf = tmp_path / "b.pdf"
    blank_pdf(pdf, 5)
    g = GeminiSummarizer("k", "m", tmp_path, client_factory=FakeClient, sleep=lambda s: None)
    out = list(g.run("pdf", pdf, ["x2"], None, lambda t: None))
    assert out[0].book == "Книга - Автор" and "FILENAME" not in out[0].markdown


def test_gemini_retries_rate_limit(tmp_path):
    slept = []
    g = GeminiSummarizer("k", "m", tmp_path, client_factory=lambda api_key: FakeClient(api_key, fail_first=2),
                         sleep=slept.append)
    assert g._generate(["x", "промпт"]).startswith("FILENAME")
    assert slept == [15, 45]


def test_custom_prompt_file(tmp_path):
    g = GeminiSummarizer("k", "m", tmp_path, client_factory=FakeClient)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "quotes.md").write_text("мои цитаты", "utf-8")
    (tmp_path / "prompt.md").write_text("мой конспект", "utf-8")
    assert g.prompt("quotes") == "мои цитаты"
    assert g.prompt("summary") == "мой конспект"
    assert "Главный герой" in g.prompt("characters")


# ---------- другие форматы книг ----------

LONG = "Это предложение повторяется, чтобы текста было достаточно. " * 10


def make_epub(path, drm=False):
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>""")
        z.writestr("OEBPS/content.opf", """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Мастер и Маргарита</dc:title><dc:creator>Михаил Булгаков</dc:creator></metadata>
<manifest><item id="c2" href="Text/ch%202.xhtml" media-type="application/xhtml+xml"/>
<item id="c1" href="Text/ch1.xhtml" media-type="application/xhtml+xml"/>
<item id="css" href="style.css" media-type="text/css"/></manifest>
<spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>""")
        z.writestr("OEBPS/Text/ch1.xhtml", f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>x</title><style>p{{}}</style></head>
<body><h1>Глава 1. Никогда не разговаривайте с неизвестными</h1><p>Однажды весною, в час небывало жаркого заката.</p><p>{LONG}</p></body></html>""")
        z.writestr("OEBPS/Text/ch 2.xhtml", """<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h2>Глава 2</h2><p>Понтий Пилат<br/>прокуратор Иудеи</p></body></html>""")
        if drm:
            z.writestr("META-INF/encryption.xml", """<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData><enc:CipherData>
<enc:CipherReference URI="OEBPS/Text/ch1.xhtml"/></enc:CipherData></enc:EncryptedData></encryption>""")


FB2 = f"""<?xml version="1.0" encoding="windows-1251"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">
<description><title-info><author><first-name>Лев</first-name><last-name>Толстой</last-name></author>
<book-title>Война и мир</book-title></title-info>
<document-info><author><nickname>scanner</nickname></author></document-info></description>
<body><title><p>Война и мир</p></title>
<section><title><p>Том первый</p></title>
<section><title><p>Часть первая</p><p>I</p></title><epigraph><p>Эпиграф</p></epigraph>
<p>— Eh bien, mon prince. Gênes et Lucques ne sont plus que des apanages.</p><empty-line/><p>{LONG}</p>
<poem><stanza><v>Строка стиха</v></stanza></poem></section></section></body>
<body name="notes"><section><p>Сноска, которую не надо включать</p></section></body>
</FictionBook>""".encode("cp1251", errors="replace")


def make_docx(path):
    import zipfile
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f"""<w:document {w}><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Введение</w:t></w:r></w:p>
<w:p><w:r><w:t xml:space="preserve">Первый </w:t></w:r><w:r><w:t>абзац.</w:t></w:r></w:p>
<w:p><w:r><w:t>{LONG}</w:t></w:r></w:p></w:body></w:document>""")
        z.writestr("docProps/core.xml", """<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Отчёт</dc:title><dc:creator>Иванов</dc:creator></cp:coreProperties>""")


def test_extract_epub(tmp_path):
    p = tmp_path / "book.epub"
    make_epub(p)
    ex = extract_text(p)
    assert (ex.title, ex.author) == ("Мастер и Маргарита", "Михаил Булгаков")
    assert ex.text.startswith("# Глава 1. Никогда не разговаривайте с неизвестными\n\nОднажды весною")
    assert "## Глава 2\n\nПонтий Пилат\nпрокуратор Иудеи" in ex.text  # порядок по spine, %20 в href
    assert "p{}" not in ex.text and "\n\nx\n" not in ex.text  # стили и <head> выброшены
    assert "автор — Михаил Булгаков" in ex.hint


def test_extract_epub_with_drm(tmp_path):
    p = tmp_path / "drm.epub"
    make_epub(p, drm=True)
    with pytest.raises(ExtractError, match="DRM"):
        extract_text(p)


def test_extract_fb2_and_fb2_zip(tmp_path):
    import zipfile
    p = tmp_path / "war.fb2"
    p.write_bytes(FB2)
    ex = extract_text(p)
    assert (ex.title, ex.author) == ("Война и мир", "Лев Толстой")
    assert "## Том первый" in ex.text and "### Часть первая I" in ex.text
    assert "Eh bien, mon prince" in ex.text  # cp1251 прочитан
    assert "Строка стиха" in ex.text and "Эпиграф" in ex.text
    assert "Сноска" not in ex.text
    z = tmp_path / "war.fb2.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("war.fb2", FB2)
    assert extract_text(z).text == ex.text


def test_extract_docx(tmp_path):
    p = tmp_path / "r.docx"
    make_docx(p)
    ex = extract_text(p)
    assert ex.text.startswith("## Введение\n\nПервый абзац.") and (ex.title, ex.author) == ("Отчёт", "Иванов")


def test_extract_txt_cp1251_and_html(tmp_path):
    t = tmp_path / "a.txt"
    t.write_bytes(("Глава первая\r\n\r\n\r\n\r\n" + LONG).encode("cp1251"))
    assert extract_text(t).text.startswith("Глава первая\n\nЭто предложение")
    h = tmp_path / "a.html"
    h.write_text(f"<html><head><script>evil()</script></head><body><nav>меню</nav><h1>Статья</h1><p>{LONG}</p></body></html>")
    text = extract_text(h).text
    assert "evil" not in text and "Это предложение" in text
    short = tmp_path / "s.txt"
    short.write_text("мало")
    with pytest.raises(ExtractError, match="200"):
        extract_text(short)


def test_split_name_and_magic():
    assert split_name("Книга.FB2.zip") == ("Книга", ".fb2.zip")
    assert split_name("a.epub") == ("a", ".epub")
    assert split_name("a.zip") == ("a.zip", "")
    assert split_name(".pdf") == (".pdf", "")
    assert looks_valid(".epub", b"PK\x03\x04rest") and not looks_valid(".epub", b"%PDF")
    assert looks_valid(".fb2", b"\xef\xbb\xbf<?xml") and looks_valid(".djvu", b"AT&TFORM")
    assert not looks_valid(".txt", b"MZ\x00\x00")


def test_upload_epub_fb2_txt_and_process(tmp_path):
    fake = FakeSummarizer()
    client, s, app = make(tmp_path, fake)
    epub = tmp_path / "src.epub"
    make_epub(epub)
    files = [("files", ("Мастер.epub", epub.read_bytes(), "application/epub+zip")),
             ("files", ("Война.fb2.zip", __import__("io").BytesIO(), "application/zip")),
             ("files", ("Заметки.txt", LONG.encode(), "text/plain")),
             ("files", ("Фейк.epub", b"not a zip", "application/epub+zip")),
             ("files", ("Программа.exe", b"MZ", "application/octet-stream"))]
    r = client.post("/add", auth=AUTH, files=files, follow_redirects=False)
    assert r.headers["location"] == "/?m=rejected"
    assert sorted(p.name for p in s.inbox.iterdir()) == ["Заметки.txt", "Мастер.epub"]
    home = client.get("/", auth=AUTH).text
    assert '<span class="job-type">EPUB</span>' in home and '<span class="job-type">TXT</span>' in home
    assert run_queue(app) == 2
    assert (s.archive / "Мастер.epub").exists()
    assert read_meta(s.books_dir / "Мастер - Автор.md")["source"] == "Мастер.epub"


def test_inbox_accepts_fb2_zip(tmp_path):
    client, s, app = make(tmp_path, FakeSummarizer())
    (s.inbox / "Война.fb2.zip").write_bytes(b"PK\x03\x04")
    (s.inbox / "readme.zip").write_bytes(b"PK\x03\x04")
    assert run_queue(app) == 1
    assert (s.books_dir / "Война - Автор.md").exists() and (s.archive / "Война.fb2.zip").exists()
    assert (s.inbox / "readme.zip").exists()  # чужие файлы не трогаем


def test_gemini_text_book_formats_and_compression(tmp_path):
    p = tmp_path / "war.fb2"
    p.write_bytes(FB2.replace(LONG.encode("cp1251"), (LONG * 200).encode("cp1251")))
    holder = {}

    def factory(api_key):
        holder["c"] = FakeClient(api_key)
        return holder["c"]

    g = GeminiSummarizer("k", "m", tmp_path, client_factory=factory, sleep=lambda s: None)
    out = list(g.run("file", p, ["key", "x4"], None, lambda t: None))
    c = holder["c"]
    assert c.uploads == []  # текстовые книги не загружаются как файлы
    first = c.requests[0][0]
    assert first.startswith("Книга (файл war.fb2).\nМетаданные файла: название — Война и мир; автор — Лев Толстой.")
    compress = c.requests[1:]
    assert len(compress) == 3 and all(isinstance(r[0], str) for r in compress)  # 16 000+ слов → 3 части
    assert out[1].fmt == "x4" and out[1].book == "Книга - Автор"


def test_djvu_is_converted(tmp_path):
    import shutil as sh
    if not sh.which("c44") or not sh.which("ddjvu"):
        pytest.skip("нет djvulibre")
    from bookhub.extract import djvu_to_pdf
    ppm = tmp_path / "page.ppm"
    ppm.write_bytes(b"P6 8 8 255\n" + b"\xff" * 192)
    djvu = tmp_path / "book.djvu"
    subprocess.run(["c44", str(ppm), str(djvu)], check=True, capture_output=True)
    pdf = djvu_to_pdf(djvu, tmp_path / "cache")
    assert pdf.read_bytes().startswith(b"%PDF")
    assert djvu_to_pdf(djvu, tmp_path / "cache") == pdf  # второй раз берётся из кэша

    fake = FakeSummarizer()
    client, s, app = make(tmp_path, fake)
    r = client.post("/add", auth=AUTH, files=[("files", ("Скан.djvu", djvu.read_bytes(), "image/vnd.djvu"))],
                    follow_redirects=False)
    assert r.headers["location"] == "/?m=added"
    assert run_queue(app) == 1
    assert fake.calls[0][1] == "Скан.djvu.pdf"  # в модель уходит PDF
    assert read_meta(s.books_dir / "Скан.djvu - Автор.md")["source"] == "Скан.djvu"


# ---------- новый интерфейс ----------

def test_live_endpoint_updates_without_reload(tmp_path):
    fake = FakeSummarizer()
    client, s, app = make(tmp_path, fake)
    assert client.get("/live").status_code == 401
    first = client.get("/live", auth=AUTH).json()
    assert first["active"] == 0 and first["notes"] == 0 and "Очередь пуста" in first["queue"]
    assert first["library"] is not None  # версия библиотеки не передана — отдаём её целиком
    same = client.get(f"/live?v={first['version']}&lib={first['lib_version']}", auth=AUTH).json()
    assert same["library"] is None and same["queue"] is None and same["version"] == first["version"]  # ничего не изменилось
    upload(client, "a.pdf", formats=["summary", "quotes"])
    queued = client.get(f"/live?lib={first['lib_version']}", auth=AUTH).json()
    assert queued["version"] != first["version"] and queued["active"] == 1 and 'data-job="1"' in queued["queue"]
    run_queue(app)
    done = client.get(f"/live?lib={first['lib_version']}", auth=AUTH).json()
    assert done["notes"] == 2 and done["active"] == 0 and done["library"] and "book-card" in done["library"]
    assert done["lib_version"] != first["lib_version"]


def test_index_page_has_redesign_parts(tmp_path):
    client, s, app = make(tmp_path, FakeSummarizer())
    html = client.get("/", auth=AUTH).text
    assert 'data-scene' in html and "<summary" not in html  # пустая библиотека: ролик открыт сразу
    assert 'data-version="' in html and 'data-drop' in html and ".fb2.zip" in html
    assert '<script type="module" src="/static/app.js?v=' in html
    upload(client, "a.pdf")
    run_queue(app)
    html = client.get("/", auth=AUTH).text
    assert "Как это работает</summary>" in html and "hero-compact" in html  # книги есть: ролик свёрнут
    assert 'class="book-card spine-' in html


def test_note_page_shows_properties(tmp_path):
    client, s, app = make(tmp_path, FakeSummarizer())
    upload(client, "Книга.pdf", formats=["x3"])
    run_queue(app)
    page = client.get("/notes/Книга - Автор (сжатие x3).md", auth=AUTH).text
    assert '<mark class="marker" data-draw>Сжатие ×3</mark>' in page and '<h1 dir="auto">Книга</h1>' in page
    assert '<dt>Источник</dt><dd>Книга.pdf</dd>' in page and '<dt>Статус</dt><dd>не прочитано</dd>' in page
    assert '<p class="byline" dir="auto">Автор</p>' in page


def test_flash_messages_have_kind(tmp_path):
    client, _, _ = make(tmp_path)
    assert 'class="flash ok"' in client.get("/?m=added", auth=AUTH).text
    assert 'class="flash warn"' in client.get("/?m=badurl", auth=AUTH).text
    assert "data-flash" not in client.get("/?m=nonsense", auth=AUTH).text


def test_static_assets_cached_and_compressed(tmp_path):
    client, _, _ = make(tmp_path)
    r = client.get("/static/vendor/motion-14.6.0.min.js", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip"
    assert "max-age=604800" in client.get("/static/fonts/mulish-cyrillic-wght-normal.woff2").headers["cache-control"]
    font = client.get("/static/fonts/mulish-cyrillic-wght-normal.woff2")
    assert font.status_code == 200 and font.content[:4] == b"wOF2"
    assert client.get("/", auth=AUTH).headers["cache-control"] == "no-cache"  # кэш «назад/вперёд» разрешён
    assert client.get("/live", auth=AUTH).headers["cache-control"] == "no-store"
    assert "immutable" in client.get("/static/style.css?v=1").headers["cache-control"]
    assert "immutable" in client.get("/static/vendor/motion-14.6.0.min.js").headers["cache-control"]
    assert client.get("/static/favicon.svg").status_code == 200


# ---------- безопасность ----------

def test_short_password_rejected(tmp_path):
    with pytest.raises(ConfigError, match="не меньше 10"):
        create_app(Settings(web_password="123456", data_dir=tmp_path), start_scheduler=False)


def test_guard_rejects_before_reading_body(tmp_path):
    client, s, app = make(tmp_path, FakeSummarizer())
    r = client.post("/add", files=[("files", ("a.pdf", PDF * 1000, "application/pdf"))])
    assert r.status_code == 401 and "Нужны логин и пароль" in r.text
    assert 'charset="UTF-8"' in r.headers["www-authenticate"]
    assert list(s.inbox.iterdir()) == [] and app.state.store.list() == []
    # слишком большой запрос отсекается по Content-Length ещё до разбора формы
    r = client.post("/add", auth=AUTH, content=b"x", headers={"content-length": str(s.max_request_bytes + 1),
                                                              "content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 413


def test_login_rate_limit(tmp_path):
    client, s, app = make(tmp_path)
    now = [1000.0]
    app.state.limiter.clock = lambda: now[0]
    for _ in range(10):
        assert client.get("/", auth=("me", "wrong-password")).status_code == 401
    r = client.get("/", auth=AUTH)
    assert r.status_code == 429 and r.headers["retry-after"] == "600"  # даже с верным паролем
    assert client.get("/health").status_code == 200  # проверка здоровья не блокируется
    now[0] += 601
    assert client.get("/", auth=AUTH).status_code == 200
    assert client.get("/").status_code == 401  # запрос без пароля не считается попыткой
    assert not app.state.limiter.blocked("testclient")


def test_markdown_cannot_inject_attributes_or_js_links():
    _, html = render_note("## Т {: onclick=alert(1) }\n\n[жми](javascript:alert(1)) [ok](https://e.com) [[Тема]]\n\n"
                          "Сноска[^1]\n\n[^1]: Текст\n")
    assert "<h2>Т {: onclick=alert(1) }</h2>" in html  # осталось текстом, а не атрибутом
    assert "<h2 " not in html and "javascript:" not in html
    assert 'href="https://e.com"' in html and 'rel="noopener noreferrer"' in html
    assert '<span class="wikilink">Тема</span>' in html and 'class="footnote"' in html


def test_null_byte_in_note_name(tmp_path):
    client, _, _ = make(tmp_path)
    assert client.get("/notes/a.md%00.md", auth=AUTH).status_code == 404


def test_zip_bomb_limit(tmp_path, monkeypatch):
    import bookhub.extract as ex
    p = tmp_path / "big.docx"
    make_docx(p)
    monkeypatch.setattr(ex, "MAX_MEMBER", 100)
    with pytest.raises(ExtractError, match="слишком большой"):
        extract_text(p)
    e = tmp_path / "book.epub"
    make_epub(e)
    monkeypatch.setattr(ex, "MAX_MEMBER", 10_000_000)
    monkeypatch.setattr(ex, "MAX_UNPACKED", 300)
    with pytest.raises(ExtractError, match="512"):
        extract_text(e)


def test_git_token_never_in_args_or_errors(tmp_path, monkeypatch):
    from bookhub import gitsync
    s = Settings(web_password="secret-pass-42", data_dir=tmp_path / "data", git_repo_url="https://github.com/u/v.git",
                 git_token="ghp_SECRET_TOKEN_123")
    s.ensure_dirs()
    g = gitsync.GitSync(s)
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        raise subprocess.TimeoutExpired(cmd, 180, output="Authorization: Basic " + g._basic())

    monkeypatch.setattr(gitsync.subprocess, "run", fake_run)
    assert g.sync("msg") is False
    assert "ghp_SECRET_TOKEN_123" not in " ".join(seen["cmd"]) and g._basic() not in " ".join(seen["cmd"])
    assert seen["env"]["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert "нет ответа" in g.last_error and "SECRET" not in g.last_error and g._basic() not in g.last_error
    assert g._mask("fatal: https://user:ghp_SECRET_TOKEN_123@github.com/u/v.git") == "fatal: https://***@github.com/u/v.git"


def test_git_conflict_prefers_vault_and_keeps_syncing(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    client, s, app = make(tmp_path, FakeSummarizer(), git_repo_url=str(remote))
    upload(client, "a.pdf")
    run_queue(app)
    # в Obsidian заметку отредактировали, а на сайте — удалили
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], check=True, capture_output=True)
    (seed / "Books" / "a - Автор.md").write_text("правка в Obsidian", "utf-8")
    for cmd in (["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "edit"], ["push", "origin", "HEAD:main"]):
        subprocess.run(["git", *cmd], cwd=seed, check=True, capture_output=True)
    client.post("/notes/a - Автор.md/delete", auth=AUTH)
    assert app.state.git.sync("Remove a") is True and app.state.git.last_error is None
    assert (s.books_dir / "a - Автор.md").read_text("utf-8") == "правка в Obsidian"  # правка не потерялась
    # и следующие синхронизации работают
    upload(client, "b.pdf")
    run_queue(app)
    assert app.state.git.last_error is None and "Books/b - Автор.md" in ls_remote(remote)


def test_books_folder_recreated_after_git_removes_it(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    client, s, app = make(tmp_path, FakeSummarizer(), git_repo_url=str(remote))
    upload(client, "a.pdf")
    run_queue(app)
    client.post("/notes/a - Автор.md/delete", auth=AUTH)
    app.state.git.sync("Remove a")
    assert s.books_dir.is_dir()
    upload(client, "b.pdf")
    assert run_queue(app) == 1


# ---------- расходы, уведомления, надёжность ----------

def test_request_cap_stops_before_spending(tmp_path):
    pdf = tmp_path / "b.pdf"
    blank_pdf(pdf, 45)  # 3 части для сжатия
    holder = {}

    def factory(api_key):
        holder["c"] = FakeClient(api_key)
        return holder["c"]

    g = GeminiSummarizer("k", "m", tmp_path, client_factory=factory, sleep=lambda s: None, max_requests=3)
    with pytest.raises(Exception, match="нужно 4 запроса|нужно 4 запросов"):
        list(g.run("pdf", pdf, ["summary", "x3"], None, lambda t: None))
    assert holder["c"].requests == [] and holder["c"].uploads == []  # ни одного платного запроса


def test_daily_limit_requeues_and_stops(tmp_path):
    from bookhub.summarizer import DailyLimitReached
    client, s, app = make(tmp_path, FakeSummarizer(), daily_request_limit=2)
    proc = app.state.processor
    proc.take_request()
    proc.take_request()
    with pytest.raises(DailyLimitReached):
        proc.take_request()

    class LimitedFake(FakeSummarizer):
        def run(self, kind, source, formats, book, progress):
            yield from super().run(kind, source, formats[:1], book, progress)
            raise DailyLimitReached("Дневной лимит запросов к Gemini исчерпан")

    proc.summarizer = LimitedFake()
    sent = []
    app.state.notifier.url, app.state.notifier.background = "https://ntfy.example/t", False
    app.state.notifier.opener = lambda req, timeout: sent.append(req) or io.BytesIO(b"ok")
    upload(client, "a.pdf", formats=["summary", "quotes"])
    upload(client, "b.pdf")
    assert run_queue(app) == 1  # одна заметка готова, потом лимит — вторая книга не трогается
    jobs = app.state.store.list()
    assert [j.status for j in jobs] == ["queued", "queued"] and jobs[0].done_list == ["summary"]
    assert len(sent) == 1 and "лимит" in urllib.parse.unquote(sent[0].data.decode())
    assert 'Дневной лимит запросов' in client.get("/", auth=AUTH).text


def test_error_notification_and_ntfy_format(tmp_path):
    client, s, app = make(tmp_path, FakeSummarizer(fail=True))
    sent = []
    n = app.state.notifier
    n.url, n.background = "https://ntfy.sh/my-topic", False
    n.opener = lambda req, timeout: sent.append(req) or io.BytesIO(b"ok")
    upload(client, "a.pdf")
    run_queue(app)
    req = sent[0]
    assert req.get_method() == "POST" and req.full_url == "https://ntfy.sh/my-topic"
    assert "не удалось обработать «a»" in req.data.decode() and "модель недоступна" in req.data.decode()
    # ссылка с {text} — GET, текст подставляется в адрес
    from bookhub.notify import Notifier, mask_url
    got = []
    tg = Notifier("https://api.telegram.org/bot123:ABC/sendMessage?chat_id=1&text={text}",
                  opener=lambda req, timeout: got.append(req) or io.BytesIO(b"ok"), background=False)
    tg.send("готово 1")
    assert got[0].get_method() == "GET" and "text=Book%20Hub%3A%20" in got[0].full_url
    assert "123:ABC" not in mask_url(tg.url)


def test_trash_undo_and_purge(tmp_path):
    client, s, app = make(tmp_path, FakeSummarizer())
    upload(client, "a.pdf")
    run_queue(app)
    r = client.post("/notes/a - Автор.md/delete", auth=AUTH, follow_redirects=False)
    assert r.status_code == 303 and "undo=" in r.headers["location"]
    assert not (s.books_dir / "a - Автор.md").exists()
    page = client.get(r.headers["location"], auth=AUTH).text
    item = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)["undo"][0]
    assert f'action="/trash/{item}/restore"' in page
    assert client.post(f"/trash/{item}/restore", auth=AUTH, follow_redirects=False).headers["location"] == "/?m=restored"
    assert (s.books_dir / "a - Автор.md").exists()
    assert client.post("/trash/../../etc/restore", auth=AUTH).status_code == 404
    # удалённый из очереди файл тоже в корзине, а не стёрт
    upload(client, "b.pdf")
    job = app.state.store.list("queued")[0]
    client.post(f"/jobs/{job.id}/delete", auth=AUTH)
    assert not (s.inbox / "b.pdf").exists() and len(app.state.trash.items()) == 1
    assert app.state.trash.purge(older_than_days=30) == 0  # свежее не трогаем
    client.post("/trash/empty", auth=AUTH)
    assert app.state.trash.items() == []


def test_export_zip(tmp_path):
    import zipfile
    client, s, app = make(tmp_path, FakeSummarizer())
    upload(client, "a.pdf", formats=["summary", "quotes"])
    run_queue(app)
    r = client.get("/export.zip", auth=AUTH)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert sorted(names) == ["Books/a - Автор (цитаты).md", "Books/a - Автор.md"]


def test_backup_and_restore_roundtrip(tmp_path):
    from bookhub.backup import list_backups, make_backup, restore
    client, s, app = make(tmp_path, FakeSummarizer(), backup_keep=2)
    upload(client, "a.pdf")
    run_queue(app)
    upload(client, "b.pdf")  # в очереди
    archive = make_backup(s, include_books=True)
    assert archive.exists()
    fresh = Settings(web_password="secret-pass-42", data_dir=tmp_path / "restored")
    restore(fresh, archive)
    for _ in range(3):
        make_backup(s)
    assert len(list_backups(s)) == 2  # хранится не больше BACKUP_KEEP
    client2 = TestClient(create_app(fresh, summarizer=FakeSummarizer(), start_scheduler=False))
    home = client2.get("/", auth=AUTH).text
    assert 'data-title="a - автор"' in home and 'data-job="2"' in home
    assert (fresh.archive / "a.pdf").exists() and (fresh.inbox / "b.pdf").exists()
    with pytest.raises(ConfigError, match="--force"):
        restore(fresh, list_backups(s)[0])


def test_maintenance(tmp_path):
    import os
    client, s, app = make(tmp_path, FakeSummarizer())
    s.cache.mkdir(parents=True, exist_ok=True)
    (s.cache / "gone.djvu.pdf").write_bytes(PDF)  # книги уже нет в архиве
    old = app.state.trash.put((s.inbox / "x.txt").write_text("x") and s.inbox / "x.txt")
    os.utime(s.trash / old, (0, 0))
    report = app.state.processor.maintenance()
    assert report["backup"].startswith("bookhub-") and report["trash_purged"] == 1 and report["cache_purged"] == 1


def test_health_reports_problems(tmp_path, monkeypatch):
    client, s, app = make(tmp_path)

    def broken(key):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(app.state.store, "get_meta", broken)
    r = client.get("/health")
    assert r.status_code == 503 and r.json()["db"] is False


def test_inbox_name_collision_is_renamed(tmp_path):
    client, s, app = make(tmp_path, FakeSummarizer())
    upload(client, "x.pdf")
    run_queue(app)  # x.pdf теперь в архиве, задача готова
    (s.inbox / "x.pdf").write_bytes(PDF)  # другую книгу с тем же именем положили вручную
    app.state.processor.sync_inbox()
    assert (s.inbox / "x (2).pdf").exists() and app.state.store.list("queued")[0].source == "x (2).pdf"


def test_more_formats_uses_right_book_after_name_reuse(tmp_path):
    fake = FakeSummarizer(fail=True)
    client, s, app = make(tmp_path, fake)
    upload(client, "a.pdf")
    run_queue(app)  # упала, файл книги A уже в архиве как a.pdf
    client.post(f"/jobs/{app.state.store.list()[0].id}/delete", auth=AUTH)
    fake.fail = False
    upload(client, "a.pdf")  # другая книга B с тем же именем
    run_queue(app)
    note = next(s.books_dir.glob("*.md"))
    assert read_meta(note)["source"] == "a (2).pdf"  # не a.pdf — это файл книги A


def test_same_title_different_files_are_different_books(tmp_path):
    class SameName(FakeSummarizer):
        def run(self, kind, source, formats, book, progress):
            for fmt in formats:
                yield Summary(fmt, book or "Неизвестно - Неизвестно", "текст\n")

    client, s, app = make(tmp_path, SameName())
    upload(client, "one.pdf")
    upload(client, "two.pdf")
    run_queue(app)
    html = client.get("/", auth=AUTH).text
    assert html.count('class="book-card') == 2


def test_very_long_unicode_names(tmp_path):
    class LongName(FakeSummarizer):
        def run(self, kind, source, formats, book, progress):
            for fmt in formats:
                yield Summary(fmt, "書" * 120 + " - " + "😀" * 50, "текст\n")

    client, s, app = make(tmp_path, LongName())
    r = upload(client, "道" * 120 + ".pdf", formats=["summary", "x2"])
    assert r.status_code == 303
    assert run_queue(app) == 2
    names = [p.name for p in s.books_dir.glob("*.md")]
    assert len(names) == 2 and all(len(n.encode()) <= 255 for n in names)
    assert any(n.endswith("(сжатие x2).md") for n in names)  # формат в имени не обрезан


def test_safe_filename_normalizes_unicode():
    import unicodedata
    nfd = unicodedata.normalize("NFD", "Ёжик")
    assert safe_filename(nfd) == "Ёжик"
    assert safe_filename("abc‮dcba") == "abcdcba"


def test_concurrent_uploads_with_same_name(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    client, s, app = make(tmp_path, FakeSummarizer())
    big = PDF + b"0" * (3 * 1024 * 1024)
    with ThreadPoolExecutor(4) as pool:
        codes = list(pool.map(lambda _: upload(client, "same.pdf", big).status_code, range(4)))
    assert codes == [303] * 4
    assert sorted(p.name for p in s.inbox.iterdir()) == ["same (2).pdf", "same (3).pdf", "same (4).pdf", "same.pdf"]
    assert len(app.state.store.list()) == 4


def test_migration_is_atomic_and_backed_up(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as c:
        c.execute("""CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL CHECK (kind IN ('pdf','url')),
                     source TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', error TEXT, note TEXT,
                     created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        for i in range(5):
            c.execute("INSERT INTO jobs (kind, source, title, created_at, updated_at) VALUES ('pdf',?,?,'t','t')", (f"{i}.pdf", str(i)))
        c.execute("DELETE FROM jobs WHERE id=5")  # последний номер 5 не должен выдаваться заново
    real = JobStore._upgrade

    def crash(self, c):
        real(self, c)
        raise RuntimeError("сбой посреди миграции")

    monkeypatch.setattr(JobStore, "_upgrade", crash)
    with pytest.raises(RuntimeError):
        JobStore(db)
    with sqlite3.connect(db) as c:  # база осталась в старом виде
        assert c.execute("PRAGMA user_version").fetchone()[0] == 0
        assert {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {"jobs"}
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 4
    monkeypatch.setattr(JobStore, "_upgrade", real)
    store = JobStore(db)
    assert (tmp_path / "old.db.bak-v0").exists()
    assert store.add("file", "new.epub", "n").id == 6
    with sqlite3.connect(db) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 3
        idx = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"jobs_status", "jobs_source"} <= idx


def test_claim_is_atomic_between_stores(tmp_path):
    db = tmp_path / "q.db"
    a, b = JobStore(db), JobStore(db)
    a.add("file", "1.pdf", "1")
    a.add("file", "2.pdf", "2")
    first, second, third = a.claim_next(), b.claim_next(), a.claim_next()
    assert {first.id, second.id} == {1, 2} and third is None


def test_scheduler_lock_is_exclusive(tmp_path):
    from bookhub.main import acquire_scheduler_lock
    s = Settings(web_password="secret-pass-42", data_dir=tmp_path)
    tmp_path.mkdir(exist_ok=True)
    first = acquire_scheduler_lock(s)
    assert first and acquire_scheduler_lock(s) is None
    first.close()
    assert acquire_scheduler_lock(s)


def test_bad_timezone_rejected(tmp_path):
    with pytest.raises(ConfigError, match="часовой пояс"):
        Settings(web_password="secret-pass-42", data_dir=tmp_path, timezone="Moscow").validate()


def test_library_cache_sees_changes(tmp_path):
    import os
    from bookhub.library import library
    d = tmp_path / "Books"
    d.mkdir()
    p = d / "n.md"
    p.write_text('---\nbook: "A - B"\nformat: "summary"\n---\nx', "utf-8")
    assert library(d)[0].notes[0].fmt == "summary"
    p.write_text('---\nbook: "A - B"\nformat: "quotes"\n---\nxx', "utf-8")
    os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 1_000_000))
    assert library(d)[0].notes[0].fmt == "quotes"
    p.unlink()
    assert library(d) == []


def test_long_frontmatter_is_read():
    from bookhub.library import parse_meta
    head = "---\n" + "".join(f"k{i}: {'x' * 100}\n" for i in range(60)) + 'book: "A - B"\n---\n'
    assert parse_meta(head)["book"] == "A - B"


def test_stale_local_copy_does_not_overwrite_vault_edits(tmp_path):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(seed)], check=True, capture_output=True)
    (seed / "Books").mkdir()
    (seed / "Books" / "a.md").write_text("свежая правка из Obsidian", "utf-8")
    for cmd in (["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "seed"], ["push", "origin", "HEAD:main"]):
        subprocess.run(["git", *cmd], cwd=seed, check=True, capture_output=True)
    client, s, app = make(tmp_path, FakeSummarizer(), git_repo_url=str(remote))
    (s.books_dir / "a.md").write_text("старая копия из восстановленной папки", "utf-8")  # без .git
    upload(client, "n.pdf")
    run_queue(app)
    assert app.state.git.last_error is None
    show = subprocess.run(["git", "--git-dir", str(remote), "show", "main:Books/a.md"], capture_output=True, text=True).stdout
    assert show == "свежая правка из Obsidian" and (s.books_dir / "a.md").read_text("utf-8") == show
    assert "Books/n - Автор.md" in ls_remote(remote)



def test_log_times_use_timezone(tmp_path):
    import logging
    import time as _time
    from bookhub.main import setup_logging
    setup_logging(Settings(web_password="secret-pass-42", data_dir=tmp_path, timezone="Asia/Tokyo"))
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
    record.created = 0  # 1970-01-01 00:00 UTC = 09:00 в Токио
    assert logging.Formatter("%(asctime)s").format(record).startswith("1970-01-01 09:00:00")
    assert _time.localtime  # стандартный converter не сломан для других модулей



def test_human_when():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from bookhub.main import human_when
    tz = ZoneInfo("Europe/Saratov")
    now = datetime.now(tz)
    assert human_when(now.strftime("%Y-%m-%d") + " 09:05", tz) == "сегодня в 09:05"
    assert human_when((now - timedelta(days=1)).strftime("%Y-%m-%d") + " 23:59", tz) == "вчера в 23:59"
    assert human_when("2020-03-06 07:00", tz) == "6 марта 2020 в 07:00"
    assert human_when(None, tz) == "" and human_when("непонятно", tz) == "непонятно"



def test_error_pages_are_styled_for_browsers(tmp_path):
    client, _, _ = make(tmp_path)
    html = client.get("/notes/nope.md", auth=AUTH, headers={"Accept": "text/html"})
    assert html.status_code == 404 and "Заметка не найдена" in html.text and 'class="error-page"' in html.text
    assert client.get("/notes/nope.md", auth=AUTH).json() == {"detail": "Заметка не найдена"}  # скриптам — JSON
    missing = client.get("/no-such-page", auth=AUTH, headers={"Accept": "text/html"})
    assert missing.status_code == 404 and "Такой страницы нет" in missing.text
    bad = client.post("/jobs/abc/retry", auth=AUTH, headers={"Accept": "text/html"})
    assert bad.status_code == 422 and "Форма заполнена неверно" in bad.text



def test_server_error_page(tmp_path, monkeypatch):
    s = Settings(web_password="secret-pass-42", data_dir=tmp_path / "data")
    app = create_app(s, start_scheduler=False)
    client = TestClient(app, raise_server_exceptions=False)
    monkeypatch.setattr(app.state.store, "list", lambda *a: 1 / 0)
    r = client.get("/", auth=AUTH, headers={"Accept": "text/html"})
    assert r.status_code == 500 and "Ошибка на сервере" in r.text and "ZeroDivisionError" not in r.text



def test_manifest_and_icons_are_public(tmp_path):
    client, _, _ = make(tmp_path)
    m = client.get("/static/manifest.webmanifest")
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
    assert m.json()["display"] == "standalone"
    for icon in m.json()["icons"]:
        r = client.get(icon["src"])
        assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert 'rel="manifest"' in client.get("/", auth=AUTH).text



def test_note_page_copy_without_hidden_copy_and_render_cache(tmp_path):
    from bookhub import main as m
    client, s, app = make(tmp_path, FakeSummarizer())
    upload(client, "a.pdf")
    run_queue(app)
    page = client.get("/notes/a - Автор.md", auth=AUTH).text
    assert 'data-copy-url="/notes/a%20-%20%D0%90%D0%B2%D1%82%D0%BE%D1%80.md/download"' in page and "<textarea" not in page
    assert any(k[0].endswith("a - Автор.md") for k in m._render_cache)
    note = s.books_dir / "a - Автор.md"
    note.write_text(note.read_text("utf-8") + "\nНовая строка про изменения\n", "utf-8")
    assert "Новая строка про изменения" in client.get("/notes/a - Автор.md", auth=AUTH).text  # кэш сбросился



# ---------- исправления по независимой проверке кода ----------

def test_cyrillic_login_and_password(tmp_path):
    s = Settings(web_password="секретный-пароль", web_user="я", data_dir=tmp_path / "data")
    app = create_app(s, start_scheduler=False)
    client = TestClient(app)
    import base64 as b64
    good = {"Authorization": "Basic " + b64.b64encode("я:секретный-пароль".encode()).decode()}
    assert client.get("/", headers=good).status_code == 200
    assert client.get("/live", headers=good).status_code == 200
    bad = {"Authorization": "Basic " + b64.b64encode("я:неверный".encode()).decode()}
    assert client.get("/", headers=bad).status_code == 401


def test_failed_login_is_slowed_and_not_reset_by_success(tmp_path):
    import time as _time
    client, s, app = make(tmp_path)
    app.state.limiter.fail_delay = 0.2
    started = _time.monotonic()
    assert client.get("/", auth=("me", "wrong-password")).status_code == 401
    assert _time.monotonic() - started >= 0.2
    app.state.limiter.fail_delay = 0
    for _ in range(9):
        client.get("/", auth=("me", "wrong-password"))
        client.get("/live", auth=AUTH)  # вкладка владельца продолжает опрашивать сервер
    assert client.get("/", auth=AUTH).status_code == 429  # счётчик подбирающего не обнулился


def _gemini_app(tmp_path, **kw):
    holder = {}

    def factory(api_key):
        holder["c"] = FakeClient(api_key)
        return holder["c"]

    g = GeminiSummarizer("k", "m", tmp_path, client_factory=factory, sleep=lambda s: None)
    client, s, app = make(tmp_path, g, **kw)
    return client, s, app, holder


def test_book_bigger_than_daily_limit_fails_without_spending(tmp_path):
    client, s, app, holder = _gemini_app(tmp_path, daily_request_limit=3)
    pdf = tmp_path / "big.pdf"
    blank_pdf(pdf, 45)  # 1 конспект + 3 части сжатия = 4 запроса > 3
    upload(client, "big.pdf", pdf.read_bytes(), formats=["summary", "x3"])
    run_queue(app)
    job = app.state.store.list()[0]
    assert job.status == "error" and "больше дневного лимита 3" in job.error
    assert holder["c"].requests == [] and app.state.store.requests_on(app.state.processor.today()) == 0


def test_not_enough_left_today_postpones_and_takes_next_book(tmp_path):
    client, s, app, holder = _gemini_app(tmp_path, daily_request_limit=10)
    proc = app.state.processor
    for _ in range(8):
        proc.take_request()  # сегодня уже потрачено 8 из 10
    pdf = tmp_path / "big.pdf"
    blank_pdf(pdf, 45)
    upload(client, "big.pdf", pdf.read_bytes(), formats=["summary", "x3"])  # нужно 4
    small = tmp_path / "small.txt"
    small.write_text("Текст небольшой книги. " * 40, "utf-8")
    upload(client, "small.txt", small.read_bytes(), formats=["key"])  # нужен 1
    assert run_queue(app, limit=1) == 1  # большая отложена без трат, маленькая сделана
    big, little = app.state.store.list()
    assert big.status == "queued" and "продолжу завтра" in (big.progress or "") and big.done_list == []
    assert little.status == "done" and app.state.store.requests_on(proc.today()) == 9
    assert "продолжу завтра" in client.get("/", auth=AUTH).text


def test_non_utf8_file_name_in_inbox(tmp_path):
    import os
    client, s, app = make(tmp_path, FakeSummarizer())
    name = "Книга про лес.txt".encode("cp1251")
    with open(os.path.join(os.fsencode(s.inbox), name), "wb") as f:
        f.write(("Текст книги. " * 40).encode())
    assert client.get("/", auth=AUTH).status_code == 200  # раньше — 500 на каждой странице
    assert [p.name for p in s.inbox.iterdir()] == ["Книга про лес.txt"]
    assert app.state.store.list()[0].source == "Книга про лес.txt"


def test_one_line_text_is_split_and_truncated_answer_is_error(tmp_path):
    text = "Очень длинное предложение книги без пустых строк. " * 4000  # ~28 000 слов одним абзацем
    parts = split_text(text, 6000)
    assert len(parts) >= 4 and max(len(p.split()) for p in parts) <= 6000

    class Truncating(FakeClient):
        def __init__(self, api_key):
            super().__init__(api_key)
            reason = type("C", (), {"finish_reason": "FinishReason.MAX_TOKENS"})()
            self.models.generate_content = lambda model, contents: type("R", (), {"text": "обрыв", "candidates": [reason]})()

    g = GeminiSummarizer("k", "m", tmp_path, client_factory=Truncating, sleep=lambda s: None)
    with pytest.raises(Exception, match="MAX_TOKENS"):
        g._generate(["x", "prompt"])


def test_git_url_fixed_in_env_reconnects(tmp_path):
    from bookhub.gitsync import GitSync
    good = tmp_path / "remote.git"
    typo = tmp_path / "remote-typo.git"
    base = dict(web_password="secret-pass-42", data_dir=tmp_path / "data")
    s1 = Settings(**base, git_repo_url=str(typo))
    s1.ensure_dirs()
    (s1.books_dir / "a.md").write_text("заметка", "utf-8")
    g1 = GitSync(s1)
    assert g1.sync("first") is False and not (s1.vault / ".git").exists()  # .git не остался «отвязанным»
    subprocess.run(["git", "init", "--bare", "-b", "main", str(good)], check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(good), str(seed)], check=True, capture_output=True)
    (seed / "README.md").write_text("vault", "utf-8")
    for cmd in (["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "seed"], ["push", "origin", "HEAD:main"]):
        subprocess.run(["git", *cmd], cwd=seed, check=True, capture_output=True)
    g2 = GitSync(Settings(**base, git_repo_url=str(good)))
    assert g2.sync("second") is True, g2.last_error
    files = ls_remote(good)
    assert "README.md" in files and "Books/a.md" in files
    # смена адреса в .env у уже подключённого хранилища
    other = tmp_path / "other.git"
    subprocess.run(["git", "clone", "--bare", str(good), str(other)], check=True, capture_output=True)
    g3 = GitSync(Settings(**base, git_repo_url=str(other)))
    (s1.books_dir / "b.md").write_text("новая", "utf-8")
    assert g3.sync("third") is True and "Books/b.md" in ls_remote(other)


def test_restore_never_silently_deletes_newer_data(tmp_path):
    from bookhub.backup import make_backup, restore
    client, s, app = make(tmp_path, FakeSummarizer())
    upload(client, "a.pdf")
    run_queue(app)
    archive = make_backup(s)
    (s.books_dir / "newer.md").write_text("написано после копии", "utf-8")
    s.db_path.unlink()  # база «испорчена» и удалена
    with pytest.raises(ConfigError, match="obsidian_vault"):
        restore(s, archive)
    kept = restore(s, archive, force=True)
    assert (kept / "obsidian_vault" / "Books" / "newer.md").read_text("utf-8") == "написано после копии"
    assert (s.books_dir / "a - Автор.md").exists() and s.db_path.exists()


def test_more_formats_with_nothing_selected(tmp_path):
    client, s, app = make(tmp_path, FakeSummarizer())
    upload(client, "a.pdf", formats=["key"])
    run_queue(app)
    r = client.post("/notes/a - Автор (ключевые моменты).md/more", auth=AUTH, data={}, follow_redirects=False)
    assert r.headers["location"] == "/?m=nothing" and app.state.store.list("queued") == []


def test_daily_without_key_does_nothing_and_delete_cleans_inbox(tmp_path):
    client, s, app = make(tmp_path)  # без ключа
    upload(client, "a.pdf")
    asyncio.run(app.state.processor.daily())
    job = app.state.store.list()[0]
    assert job.status == "queued" and "GEMINI_API_KEY" in app.state.store.get_meta("last_daily_run")
    app.state.store.claim_next()
    app.state.store.mark_error(job.id, "что-то пошло не так")
    client.post(f"/jobs/{job.id}/delete", auth=AUTH)
    client.get("/", auth=AUTH)
    assert app.state.store.list() == [] and not (s.inbox / "a.pdf").exists()  # книга не вернулась в очередь


def test_chunked_upload_is_size_limited(tmp_path):
    client, s, app = make(tmp_path, FakeSummarizer(), max_upload_mb=1)

    def body():
        yield b"--x\r\nContent-Disposition: form-data; name=\"files\"; filename=\"a.pdf\"\r\nContent-Type: application/pdf\r\n\r\n%PDF"
        for _ in range(8):
            yield b"0" * (1024 * 1024)
        yield b"\r\n--x--\r\n"

    r = client.post("/add", auth=AUTH, content=body(), headers={"content-type": "multipart/form-data; boundary=x",
                                                                 "accept": "text/html"})
    assert r.status_code == 413 and list(s.inbox.iterdir()) == []


def test_note_with_hand_edited_words_and_code(tmp_path):
    client, s, app = make(tmp_path)
    (s.books_dir / "n.md").write_text('---\ntitle: "N"\nwords: 1 200\nsource_words: abc\n---\n\n'
                                      "Код: `a < b && c`\n\n```\nif x < 1 && y:\n```\n", "utf-8")
    page = client.get("/notes/n.md", auth=AUTH)
    assert page.status_code == 200
    assert "<code>a &lt; b &amp;&amp; c</code>" in page.text and "&amp;lt;" not in page.text


def test_maintenance_cleans_even_when_backups_are_off(tmp_path):
    import os
    client, s, app = make(tmp_path, FakeSummarizer(), backup_time="off")
    item = app.state.trash.put((s.inbox / "x.txt").write_text("x") and s.inbox / "x.txt")
    os.utime(s.trash / item, (0, 0))
    report = app.state.processor.maintenance(with_backup=False)
    assert "backup" not in report and report["trash_purged"] == 1

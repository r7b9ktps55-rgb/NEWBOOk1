import asyncio
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

AUTH = ("me", "secret")
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
    s = Settings(web_password="secret", data_dir=tmp_path / "data", **kw)
    app = create_app(s, summarizer=summarizer, start_scheduler=False)
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
    assert client.get("/", auth=("other", "secret")).status_code == 401
    r = client.get("/", auth=AUTH)
    assert r.status_code == 200 and "Book Hub" in r.text and "Сжатие ×3" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert client.get("/health").json() == {"ok": True}


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
    assert home.count('class="book"') == 1  # одна книга в библиотеке, три формата
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
    assert 'class="tag link current"' in page and "Ключевые моменты" in page


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
    compress = [r for r in c.requests if isinstance(r[0], dict)]
    assert len(compress) == 3 and compress[0][0]["inline_data"]["mime_type"] == "application/pdf"
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
    assert '<span class="kind">EPUB</span>' in home and '<span class="kind">TXT</span>' in home
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

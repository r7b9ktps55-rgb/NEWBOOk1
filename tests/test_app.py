import asyncio
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bookhub.config import ConfigError, Settings
from bookhub.main import create_app, render_note
from bookhub.summarizer import GeminiSummarizer, Summary, add_source, parse_output, safe_filename

AUTH = ("me", "secret")
PDF = b"%PDF-1.4\n%fake\n"


class FakeSummarizer:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def summarize_pdf(self, path: Path) -> Summary:
        self.calls.append(path.name)
        if self.fail:
            raise RuntimeError("модель недоступна")
        return Summary(f"{path.stem} - Автор", f"---\ntitle: \"{path.stem}\"\n---\n\n> [!abstract] Идея\n> Текст\n\n- [ ] шаг\n")

    def summarize_url(self, url: str) -> Summary:
        self.calls.append(url)
        return Summary("Статья - Кто-то", "# Статья\n<script>alert(1)</script>\n[[Тема]]\n")


def make(tmp_path, summarizer=None, **kw):
    s = Settings(web_password="secret", data_dir=tmp_path / "data", **kw)
    app = create_app(s, summarizer=summarizer, start_scheduler=False)
    return TestClient(app), s, app


def run_queue(app, limit=None):
    return asyncio.run(app.state.processor.run(limit))


def test_password_required(tmp_path):
    with pytest.raises(ConfigError):
        create_app(Settings(web_password="", data_dir=tmp_path), start_scheduler=False)


def test_auth(tmp_path):
    client, _, _ = make(tmp_path)
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("me", "wrong")).status_code == 401
    assert client.get("/", auth=("other", "secret")).status_code == 401
    r = client.get("/", auth=AUTH)
    assert r.status_code == 200 and "Book Hub" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert client.get("/health").json() == {"ok": True}


def test_upload_process_and_view(tmp_path):
    fake = FakeSummarizer()
    client, s, app = make(tmp_path, fake)
    r = client.post("/upload", auth=AUTH, files=[("files", ("Моя книга.pdf", PDF, "application/pdf"))], follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?m=uploaded"
    assert (s.inbox / "Моя книга.pdf").exists()
    assert "Моя книга" in client.get("/", auth=AUTH).text

    assert run_queue(app) == 1
    note = s.books_dir / "Моя книга - Автор.md"
    assert note.exists()
    assert 'source: "Моя книга.pdf"' in note.read_text("utf-8")
    assert not (s.inbox / "Моя книга.pdf").exists()
    assert (s.archive / "Моя книга.pdf").exists()

    page = client.get("/notes/Моя книга - Автор.md", auth=AUTH)
    assert page.status_code == 200 and "obsidian://new?" in page.text and "☐" in page.text
    dl = client.get("/notes/Моя книга - Автор.md/download", auth=AUTH)
    assert dl.status_code == 200 and dl.content.startswith(b"---")

    # Повторная загрузка файла с тем же именем не теряется
    client.post("/upload", auth=AUTH, files=[("files", ("Моя книга.pdf", PDF, "application/pdf"))])
    assert (s.inbox / "Моя книга (2).pdf").exists()
    assert run_queue(app) == 1
    assert len(list(s.books_dir.glob("*.md"))) == 2


def test_same_title_gets_unique_note(tmp_path):
    from bookhub.worker import unique_path
    (tmp_path / "A.md").write_text("x")
    assert unique_path(tmp_path, "A", ".md").name == "A (2).md"


def test_upload_rejects_non_pdf(tmp_path):
    client, s, _ = make(tmp_path, FakeSummarizer(), max_upload_mb=1)
    r = client.post("/upload", auth=AUTH, files=[("files", ("x.pdf", b"not a pdf", "application/pdf"))], follow_redirects=False)
    assert r.headers["location"] == "/?m=rejected"
    big = PDF + b"0" * (2 * 1024 * 1024)
    client.post("/upload", auth=AUTH, files=[("files", ("big.pdf", big, "application/pdf"))])
    client.post("/upload", auth=AUTH, files=[("files", ("x.txt", PDF, "text/plain"))])
    assert list(s.inbox.iterdir()) == []


def test_upload_filename_cannot_escape_inbox(tmp_path):
    client, s, _ = make(tmp_path, FakeSummarizer())
    client.post("/upload", auth=AUTH, files=[("files", ("../../evil.pdf", PDF, "application/pdf"))])
    assert not (tmp_path / "evil.pdf").exists()
    assert [p.name for p in s.inbox.iterdir()] == ["evil.pdf"]


def test_note_path_traversal(tmp_path):
    client, s, _ = make(tmp_path)
    (s.data_dir / "secret.md").write_text("x")
    for name in ["..%2F..%2Fsecret.md", "..%2Fsecret.md", "nope.md", "bookhub.db"]:
        assert client.get(f"/notes/{name}", auth=AUTH).status_code == 404


def test_url_job_and_html_is_escaped(tmp_path):
    client, s, app = make(tmp_path, FakeSummarizer())
    assert client.post("/url", auth=AUTH, data={"url": "javascript:alert(1)"}, follow_redirects=False).headers["location"] == "/?m=badurl"
    client.post("/url", auth=AUTH, data={"url": "https://example.com/a"})
    assert run_queue(app) == 1
    page = client.get("/notes/Статья - Кто-то.md", auth=AUTH).text
    assert "<script>alert(1)" not in page
    assert '<span class="wikilink">Тема</span>' in page


def test_errors_are_kept_and_retry(tmp_path):
    fake = FakeSummarizer(fail=True)
    client, s, app = make(tmp_path, fake)
    client.post("/upload", auth=AUTH, files=[("files", ("a.pdf", PDF, "application/pdf"))])
    assert run_queue(app) == 0
    job = app.state.store.list()[0]
    assert job.status == "error" and "недоступна" in job.error
    assert "недоступна" in client.get("/", auth=AUTH).text
    fake.fail = False
    client.post(f"/jobs/{job.id}/retry", auth=AUTH)
    assert run_queue(app) == 1


def test_delete_queued_pdf_removes_file(tmp_path):
    client, s, app = make(tmp_path, FakeSummarizer())
    client.post("/upload", auth=AUTH, files=[("files", ("a.pdf", PDF, "application/pdf"))])
    job = app.state.store.list()[0]
    client.post(f"/jobs/{job.id}/delete", auth=AUTH)
    assert app.state.store.list() == [] and list(s.inbox.iterdir()) == []
    client.get("/", auth=AUTH)  # не должно вернуться через inbox
    assert app.state.store.list() == []


def test_inbox_folder_is_picked_up(tmp_path):
    client, s, app = make(tmp_path, FakeSummarizer())
    (s.inbox / "dropped.PDF").write_bytes(PDF)
    assert run_queue(app) == 1
    assert (s.books_dir / "dropped - Автор.md").exists()


def test_cross_site_post_is_blocked(tmp_path):
    client, _, _ = make(tmp_path)
    r = client.post("/url", auth=AUTH, data={"url": "https://e.com"}, headers={"origin": "https://evil.com"})
    assert r.status_code == 403
    r = client.post("/url", auth=AUTH, data={"url": "https://e.com"}, headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303


def test_run_without_key(tmp_path):
    client, _, _ = make(tmp_path)
    r = client.post("/run", auth=AUTH, follow_redirects=False)
    assert r.headers["location"] == "/?m=nokey"


def test_git_sync_to_bare_repo(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    client, s, app = make(tmp_path, FakeSummarizer(), git_repo_url=str(remote))
    client.post("/upload", auth=AUTH, files=[("files", ("g.pdf", PDF, "application/pdf"))])
    assert run_queue(app) == 1
    git = app.state.git
    assert git.last_error is None and git.last_sync
    files = subprocess.run(["git", "-c", "core.quotepath=off", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                           capture_output=True, text=True, check=True).stdout
    assert "Books/g - Автор.md" in files


def test_git_sync_joins_existing_vault(tmp_path):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(seed)], check=True, capture_output=True)
    (seed / "Existing.md").write_text("old note")
    for cmd in (["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "seed"], ["push", "origin", "HEAD:main"]):
        subprocess.run(["git", *cmd], cwd=seed, check=True, capture_output=True)
    client, s, app = make(tmp_path, FakeSummarizer(), git_repo_url=str(remote))
    client.post("/upload", auth=AUTH, files=[("files", ("n.pdf", PDF, "application/pdf"))])
    run_queue(app)
    assert app.state.git.last_error is None
    files = subprocess.run(["git", "-c", "core.quotepath=off", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                           capture_output=True, text=True, check=True).stdout
    assert "Existing.md" in files and "Books/n - Автор.md" in files


def test_parse_output():
    s = parse_output("**FILENAME: Атомные привычки - Джеймс Клир**\n---\ntitle: x\n---\nтекст", "fb")
    assert s.title == "Атомные привычки - Джеймс Клир" and s.markdown.startswith("---")
    s = parse_output("```markdown\nFILENAME: A/B: C\nbody\n```", "fb")
    assert s.title == "AB C" and s.markdown == "body\n"
    assert parse_output("no header", "fallback").title == "fallback"


def test_safe_filename():
    assert safe_filename("../a/b") == "ab"
    assert safe_filename('  "?*  ') == "Без названия"
    assert safe_filename("Книга #1 [часть]") == "Книга 1 часть"
    assert len(safe_filename("x" * 500)) == 100


def test_add_source():
    assert add_source("---\ntitle: a\n---\nbody", "https://x").startswith('---\ntitle: a\nsource: "https://x"\n---\nbody')
    assert add_source("body", "f.pdf").startswith('---\nsource: "f.pdf"\n---')
    assert add_source("---\nsource: y\n---\n", "z") == "---\nsource: y\n---\n"


def test_render_note_strips_frontmatter():
    front, html = render_note("---\ntitle: a\n---\n\n- first\n- [x] done\n")
    assert front == "title: a" and "<li>first</li>" in html and "☑" in html


def test_gemini_summarizer_with_fake_client(tmp_path):
    class Files:
        def upload(self, file, config):
            return type("F", (), {"name": "files/1", "state": type("S", (), {"name": "ACTIVE"})()})()

        def delete(self, name):
            self.deleted = name

    class Models:
        def generate_content(self, model, contents):
            assert "{date}" not in contents[-1]
            return type("R", (), {"text": "FILENAME: Книга - Автор\n---\ntitle: Книга\n---\n"})()

    class Client:
        def __init__(self, api_key):
            self.files, self.models = Files(), Models()

    pdf = tmp_path / "b.pdf"
    pdf.write_bytes(PDF)
    g = GeminiSummarizer("k", "m", "Europe/Saratov", tmp_path / "prompt.md", client_factory=Client)
    out = g.summarize_pdf(pdf)
    assert out.title == "Книга - Автор" and g.client.files.deleted == "files/1"
    (tmp_path / "prompt.md").write_text("мой промпт {date}")
    assert g.prompt().startswith("мой промпт 20")

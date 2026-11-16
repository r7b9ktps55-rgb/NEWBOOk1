"""Чтение книг разных форматов.

PDF отправляется в Gemini как есть (модель видит и текст, и сканы).
DjVu конвертируется в PDF через ddjvu (пакет djvulibre).
EPUB, FB2, DOCX, TXT, Markdown и HTML превращаются в текст здесь, локально.
"""

from __future__ import annotations

import logging
import posixpath
import re
import shutil
import subprocess
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Расширение → подпись в интерфейсе. Порядок важен: .fb2.zip проверяется раньше .zip-подобных.
FORMATS = {
    ".pdf": "PDF",
    ".epub": "EPUB",
    ".fb2.zip": "FB2",
    ".fb2": "FB2",
    ".djvu": "DjVu",
    ".djv": "DjVu",
    ".docx": "DOCX",
    ".txt": "TXT",
    ".md": "MD",
    ".html": "HTML",
    ".htm": "HTML",
}
ZIP_MAGIC = b"PK\x03\x04"
MIN_TEXT = 200
MAX_MEMBER = 128 * 1024 * 1024    # один файл внутри EPUB/DOCX/FB2.ZIP после распаковки
MAX_UNPACKED = 512 * 1024 * 1024  # все главы EPUB вместе
# Разбор HTML/XML занимает в памяти в десятки раз больше самого текста: плотная глава в 128 МБ
# потребовала бы гигабайты. Настоящие главы — сотни килобайт, книги FB2 — единицы мегабайт.
MAX_HTML = 8 * 1024 * 1024
MAX_XML = 64 * 1024 * 1024
MAX_DJVU_PDF = 2 * 1024 * 1024 * 1024


class ExtractError(Exception):
    pass


class TooLarge(ExtractError):
    """Часть книги слишком велика для безопасного разбора — книгу не пропускаем молча."""


@dataclass
class Extracted:
    text: str
    title: str = ""
    author: str = ""

    @property
    def hint(self) -> str:
        """Подсказка модели из метаданных файла."""
        parts = []
        if self.title:
            parts.append(f"название — {self.title}")
        if self.author:
            parts.append(f"автор — {self.author}")
        return "Метаданные файла: " + "; ".join(parts) + "." if parts else ""


def split_name(name: str) -> tuple[str, str]:
    """«Книга.fb2.zip» → («Книга», «.fb2.zip»). Для неизвестного формата расширение пустое."""
    lower = name.lower()
    for ext in FORMATS:
        if lower.endswith(ext) and len(name) > len(ext):
            return name[: -len(ext)], ext
    return name, ""


def label(name: str) -> str:
    return FORMATS.get(split_name(name)[1], "Файл")


def looks_valid(ext: str, head: bytes) -> bool:
    """Быстрая проверка первых байтов файла: соответствует ли содержимое расширению."""
    if ext == ".pdf":
        return head.startswith(b"%PDF")
    if ext in (".epub", ".docx", ".fb2.zip"):
        return head.startswith(ZIP_MAGIC)
    if ext in (".djvu", ".djv"):
        return head.startswith(b"AT&TFORM")
    if ext == ".fb2":
        return head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<")
    return b"\x00" not in head  # текстовые форматы


# ---------- общее ----------

def clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(data[:2_000_000]).best()
        if best and best.encoding:
            return data.decode(best.encoding, errors="replace")
    except ImportError:
        pass
    return data.decode("cp1251", errors="replace")  # самый частый случай для русских .txt


def _read_member(z: zipfile.ZipFile, name: str, budget: list[int] | None = None) -> bytes:
    """Читает файл из архива, не давая «zip-бомбе» развернуться в гигабайты в памяти."""
    size = z.getinfo(name).file_size  # zipfile не распакует больше заявленного размера
    if size > MAX_MEMBER:
        raise ExtractError("Файл внутри архива слишком большой после распаковки (больше 128 МБ)")
    if budget is not None:
        budget[0] -= size
        if budget[0] < 0:
            raise ExtractError("Книга слишком большая после распаковки (больше 512 МБ)")
    return z.read(name)


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _xml(data: bytes):
    from lxml import etree

    if len(data) > MAX_XML:
        raise TooLarge(f"XML внутри книги слишком большой для разбора (больше {MAX_XML // 1048576} МБ)")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=True)
    root = etree.fromstring(data, parser)
    if root is None:
        raise ExtractError("Не удалось разобрать XML")
    return root


BLOCKS = {"p", "div", "li", "tr", "blockquote", "section", "article", "pre", "dd", "dt", "figcaption", "table"}
HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "###", "h5": "###", "h6": "###"}


def _is_note_mark(el) -> bool:
    """Номер сноски или обратная ссылка из сноски («↩︎»)."""
    text = (el.text_content() or "").strip()
    if _local(el.tag) == "sup":
        return bool(re.fullmatch(r"\d{1,3}", text))
    marks = " ".join(f"{k}={v}" for k, v in el.attrib.items())
    return bool(re.search(r"footnote|noteref|backlink|doc-noteref", marks)) and (text.isdigit() or text in ("↩", "↩︎", ""))


def html_to_text(data: bytes) -> str:
    """HTML/XHTML → текст с абзацами и заголовками Markdown."""
    from lxml import html as lhtml

    if len(data) > MAX_HTML:
        raise TooLarge(f"Глава книги слишком большая для разбора (больше {MAX_HTML // 1048576} МБ текста)")
    # Кодировку определяем сами: без объявления lxml читает документ как latin-1.
    markup = re.sub(r"^\s*<\?xml[^>]*\?>", "", decode(data))
    if not markup.strip():
        raise ExtractError("Пустой HTML")
    try:
        doc = lhtml.fromstring(markup)
    except Exception as exc:  # пустой или битый документ
        raise ExtractError(f"Не удалось разобрать HTML: {exc}") from exc
    for bad in doc.xpath("//script|//style|//head|//nav"):
        bad.drop_tree()
    for el in doc.iter():  # в HTML перевод строки внутри текста — это просто пробел
        if el.text:
            el.text = re.sub(r"\s+", " ", el.text)
        if el.tail:
            el.tail = re.sub(r"\s+", " ", el.tail)
    for el in list(doc.iter()):  # список: ниже из дерева удаляются элементы
        name = _local(el.tag)
        if name in HEADINGS:
            el.text = f"\n\n{HEADINGS[name]} " + (el.text or "")
            el.tail = "\n\n" + (el.tail or "")
        elif name == "li":
            el.text = "- " + (el.text or "").lstrip()
            el.tail = "\n\n" + (el.tail or "")
        elif name in ("td", "th"):
            el.tail = " | " + (el.tail or "")
        elif name == "tr":
            el.tail = "\n" + (el.tail or "")
        elif name in ("sup", "a") and _is_note_mark(el):
            number = el.text_content().strip()
            # Номер сноски — в скобках, чтобы не прилип к слову; «↩︎» обратной ссылки не нужен.
            el.text = f"[{number}]" if number.isdigit() else ""
            for child in list(el):
                el.remove(child)
        elif name in BLOCKS:
            el.tail = "\n\n" + (el.tail or "")
        elif name == "br":
            el.tail = "\n" + (el.tail or "")
    text = re.sub(r" *\| *(?=\n|$)", "", doc.text_content())  # «|» после последней ячейки строки
    # заголовок без текста («## » на отдельной строке) не нужен
    return clean(re.sub(r"^#+ *$", "", text, flags=re.M))


# ---------- форматы ----------

def read_epub(path: Path) -> Extracted:
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ExtractError("Файл EPUB повреждён") from exc
    with z:
        names = set(z.namelist())
        if "META-INF/encryption.xml" in names:
            enc = _xml(_read_member(z, "META-INF/encryption.xml"))
            uris = [el.get("URI", "") for el in enc.iter() if _local(el.tag) == "cipherreference"]
            if any(not re.search(r"\.(ttf|otf|woff2?)$", u, re.I) for u in uris):
                raise ExtractError("EPUB защищён DRM — снимите защиту или загрузите другой файл")
        try:
            container = _xml(_read_member(z, "META-INF/container.xml"))
            opf_path = next(el.get("full-path") for el in container.iter() if _local(el.tag) == "rootfile")
            opf = _xml(_read_member(z, opf_path))
        except (KeyError, StopIteration) as exc:
            raise ExtractError("В EPUB нет оглавления (container.xml/OPF)") from exc
        base = posixpath.dirname(opf_path)
        manifest = {}
        title = author = ""
        for el in opf.iter():
            name = _local(el.tag)
            if name == "item":
                href = urllib.parse.unquote(el.get("href", ""))
                manifest[el.get("id")] = (posixpath.normpath(posixpath.join(base, href)), el.get("media-type", ""))
            elif name == "title" and not title:
                title = (el.text or "").strip()
            elif name == "creator" and not author:
                author = (el.text or "").strip()
        chapters = []
        budget = [MAX_UNPACKED]
        for el in opf.iter():
            if _local(el.tag) == "itemref":
                href, media = manifest.get(el.get("idref"), ("", ""))
                if href in names and ("html" in media or href.lower().endswith((".xhtml", ".html", ".htm"))):
                    data = _read_member(z, href, budget)  # слишком большая книга — ошибка, а не пропуск
                    try:
                        chapters.append(html_to_text(data))
                    except TooLarge:
                        raise
                    except ExtractError:
                        log.warning("Пропущена глава EPUB %s", href)
    return Extracted(clean("\n\n".join(c for c in chapters if c)), title, author)


def read_fb2(path: Path) -> Extracted:
    data = path.read_bytes()
    if data.startswith(ZIP_MAGIC):  # .fb2.zip
        with zipfile.ZipFile(path) as z:
            inner = [n for n in z.namelist() if n.lower().endswith(".fb2")]
            if not inner:
                raise ExtractError("В архиве нет файла .fb2")
            data = _read_member(z, inner[0])
    root = _xml(data)
    title = author = ""
    for el in root.iter():
        name = _local(el.tag)
        if name == "book-title" and not title:
            title = (el.text or "").strip()
        elif name == "author" and not author:
            fields = {_local(c.tag): (c.text or "").strip() for c in el}
            author = " ".join(fields.get(k, "") for k in ("first-name", "middle-name", "last-name")).strip()
            author = re.sub(r"\s+", " ", author) or fields.get("nickname", "")
    out: list[str] = []

    def text_of(el) -> str:
        return re.sub(r"\s+", " ", "".join(el.itertext())).strip()

    def walk(el, depth: int) -> None:
        for child in el:
            name = _local(child.tag)
            if name == "title":
                heading = " ".join(text_of(p) for p in child if text_of(p)) or text_of(child)
                if heading:
                    out.append("#" * min(depth, 3) + " " + heading)
            elif name == "section":
                walk(child, depth + 1)
            elif name in ("p", "v", "subtitle", "text-author"):
                if text_of(child):
                    out.append(text_of(child))
            elif name in ("poem", "stanza", "cite", "epigraph", "annotation"):
                walk(child, depth)
            elif name == "table":
                rows = [" | ".join(text_of(c) for c in tr if _local(c.tag) in ("td", "th"))
                        for tr in child if _local(tr.tag) == "tr"]
                if any(rows):
                    out.append("\n".join(r for r in rows if r))

    for body in (el for el in root if _local(el.tag) == "body"):
        if body.get("name") in ("notes", "comments"):
            continue
        walk(body, 1)
    return Extracted(clean("\n\n".join(out)), title, author)


def read_docx(path: Path) -> Extracted:
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            root = _xml(_read_member(z, "word/document.xml"))
            core = _xml(_read_member(z, "docProps/core.xml")) if "docProps/core.xml" in names else None
            notes = _xml(_read_member(z, "word/footnotes.xml")) if "word/footnotes.xml" in names else None
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ExtractError("Файл DOCX повреждён") from exc

    def para_text(p) -> str:
        return "".join(t.text or "" for t in p.iter() if _local(t.tag) == "t").strip()

    def paragraphs(tree) -> list[str]:
        out = []
        for p in tree.iter():
            if _local(p.tag) == "tbl":
                rows = []
                for tr in (x for x in p.iter() if _local(x.tag) == "tr"):
                    cells = [" ".join(para_text(q) for q in tc.iter() if _local(q.tag) == "p").strip()
                             for tc in tr if _local(tc.tag) == "tc"]
                    rows.append(" | ".join(cells))
                if any(rows):
                    out.append("\n".join(r for r in rows if r.strip(" |")))
                continue
            if _local(p.tag) != "p" or any(_local(a.tag) == "tbl" for a in p.iterancestors()):
                continue
            text = para_text(p)
            if not text:
                continue
            style = next((v for s in p.iter() if _local(s.tag) == "pstyle"
                          for k, v in s.attrib.items() if _local(k) == "val"), "")
            if re.match(r"(heading|title|заголовок)", style, re.I):
                text = f"## {text}"
            elif any(_local(x.tag) == "numpr" for x in p.iter()):
                text = f"- {text}"  # пункт списка
            out.append(text)
        return out

    out = paragraphs(root)
    footnotes = paragraphs(notes) if notes is not None else []
    if footnotes:
        out += ["## Сноски", *footnotes]
    title = author = ""
    if core is not None:
        for el in core.iter():
            if _local(el.tag) == "title":
                title = (el.text or "").strip()
            elif _local(el.tag) == "creator":
                author = (el.text or "").strip()
    return Extracted(clean("\n\n".join(out)), title, author)


def read_html(path: Path) -> Extracted:
    """Книга в HTML (например, с lib.ru): берём весь текст, а не «главную статью» страницы."""
    data = path.read_bytes()
    m = re.search(rb"<title[^>]*>(.*?)</title>", data, re.I | re.S)
    title = clean(re.sub(r"<[^>]+>", "", decode(m.group(1)))) if m else ""
    a = re.search(rb"<meta[^>]+name=[\"']?(?:author|dc\.creator)[\"']?[^>]*content=[\"']([^\"']+)", data, re.I)
    author = clean(decode(a.group(1))) if a else ""
    return Extracted(html_to_text(data), title, author)


def read_text_file(path: Path) -> Extracted:
    return Extracted(clean(decode(path.read_bytes())))


READERS = {
    ".epub": read_epub,
    ".fb2": read_fb2,
    ".fb2.zip": read_fb2,
    ".docx": read_docx,
    ".html": read_html,
    ".htm": read_html,
    ".txt": read_text_file,
    ".md": read_text_file,
}


def extract_text(path: Path) -> Extracted:
    ext = split_name(path.name)[1]
    reader = READERS.get(ext)
    if not reader:
        raise ExtractError(f"Формат {ext or path.suffix} не поддерживается")
    result = reader(path)
    if len(result.text) < MIN_TEXT:
        raise ExtractError("Не удалось извлечь текст из файла (меньше 200 символов)")
    return result


def djvu_to_pdf(path: Path, cache_dir: Path) -> Path:
    """Конвертирует DjVu в PDF (один раз; результат хранится в data/cache)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"{path.name}.pdf"
    if out.is_file() and out.stat().st_mtime >= path.stat().st_mtime:
        return out
    if not shutil.which("ddjvu"):
        raise ExtractError("Для DjVu нужна программа ddjvu (пакет djvulibre-bin) — в Docker-образе она уже есть")
    tmp = out.with_suffix(".part")
    res = subprocess.run(["ddjvu", "-format=pdf", "-quality=80", str(path), str(tmp)],
                         capture_output=True, text=True, timeout=1800)
    if res.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        raise ExtractError(f"Не удалось сконвертировать DjVu: {res.stderr.strip()[:300]}")
    tmp.replace(out)
    return out

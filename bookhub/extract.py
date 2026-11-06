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


class ExtractError(Exception):
    pass


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


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _xml(data: bytes):
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=True)
    root = etree.fromstring(data, parser)
    if root is None:
        raise ExtractError("Не удалось разобрать XML")
    return root


BLOCKS = {"p", "div", "li", "tr", "blockquote", "section", "article", "pre", "dd", "dt", "figcaption", "table"}
HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "###", "h5": "###", "h6": "###"}


def html_to_text(data: bytes) -> str:
    """HTML/XHTML → текст с абзацами и заголовками Markdown."""
    from lxml import html as lhtml

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
    for el in doc.iter():
        name = _local(el.tag)
        if name in HEADINGS:
            el.text = f"\n\n{HEADINGS[name]} " + (el.text or "")
            el.tail = "\n\n" + (el.tail or "")
        elif name in BLOCKS:
            el.tail = "\n\n" + (el.tail or "")
        elif name == "br":
            el.tail = "\n" + (el.tail or "")
    text = doc.text_content()
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
            enc = _xml(z.read("META-INF/encryption.xml"))
            uris = [el.get("URI", "") for el in enc.iter() if _local(el.tag) == "cipherreference"]
            if any(not re.search(r"\.(ttf|otf|woff2?)$", u, re.I) for u in uris):
                raise ExtractError("EPUB защищён DRM — снимите защиту или загрузите другой файл")
        try:
            container = _xml(z.read("META-INF/container.xml"))
            opf_path = next(el.get("full-path") for el in container.iter() if _local(el.tag) == "rootfile")
            opf = _xml(z.read(opf_path))
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
        for el in opf.iter():
            if _local(el.tag) == "itemref":
                href, media = manifest.get(el.get("idref"), ("", ""))
                if href in names and ("html" in media or href.lower().endswith((".xhtml", ".html", ".htm"))):
                    try:
                        chapters.append(html_to_text(z.read(href)))
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
            data = z.read(inner[0])
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

    for body in (el for el in root if _local(el.tag) == "body"):
        if body.get("name") in ("notes", "comments"):
            continue
        walk(body, 1)
    return Extracted(clean("\n\n".join(out)), title, author)


def read_docx(path: Path) -> Extracted:
    try:
        with zipfile.ZipFile(path) as z:
            root = _xml(z.read("word/document.xml"))
            core = _xml(z.read("docProps/core.xml")) if "docProps/core.xml" in z.namelist() else None
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ExtractError("Файл DOCX повреждён") from exc
    out = []
    for p in root.iter():
        if _local(p.tag) != "p":
            continue
        text = "".join(t.text or "" for t in p.iter() if _local(t.tag) == "t").strip()
        if not text:
            continue
        style = next((v for s in p.iter() if _local(s.tag) == "pstyle"
                      for k, v in s.attrib.items() if _local(k) == "val"), "")
        out.append(f"## {text}" if re.match(r"(heading|title|заголовок)", style, re.I) else text)
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
    return Extracted(html_to_text(data), title)


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

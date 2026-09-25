"""Load local files (PDF, HTML, text/Markdown) into the knowledge base."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from . import pdftools
from .kb import KB

TEXT_SUFFIXES = {".txt", ".md", ".csv"}
HTML_SUFFIXES = {".html", ".htm"}


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "template", "svg"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data.strip()
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """Return (title, text) for an HTML document."""
    p = _TextExtractor()
    p.feed(html)
    lines = [ln.strip() for ln in "".join(p.parts).splitlines()]
    return p.title, "\n".join(ln for ln in lines if ln)


def ingest_pdf(kb: KB, path: Path, *, source: str | None = None, title: str = "", module: str = "") -> int:
    with pdftools.open_pdf(path) as doc:
        meta_title = (doc.metadata or {}).get("title") or ""
        return kb.add_document(
            source or str(path.resolve()),
            pdftools.page_texts(doc),
            title=title or meta_title or path.stem,
            kind="pdf",
            module=module,
        )


def ingest_file(kb: KB, path: Path, *, module: str = "", title: str = "") -> int | None:
    suffix = path.suffix.lower()
    source = str(path.resolve())
    if suffix == ".pdf":
        return ingest_pdf(kb, path, title=title, module=module)
    if suffix in HTML_SUFFIXES:
        page_title, text = html_to_text(path.read_text(encoding="utf-8", errors="replace"))
        return kb.add_document(source, [(None, text)], title=title or page_title or path.stem, kind="html", module=module)
    if suffix in TEXT_SUFFIXES:
        text = path.read_text(encoding="utf-8", errors="replace")
        return kb.add_document(source, [(None, text)], title=title or path.stem, kind="text", module=module)
    return None


def ingest_path(kb: KB, path: Path, *, module: str = "") -> list[Path]:
    """Ingest a file or every supported file under a directory."""
    files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
    done = []
    for f in files:
        if ingest_file(kb, f, module=module) is not None:
            done.append(f)
    return done

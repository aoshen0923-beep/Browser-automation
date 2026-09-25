"""Page-accurate PDF inspection.

Contest questions ask things like "how many pages", "how many figures on
page 50", "last character of the second-to-last page". These are answered
deterministically here instead of trusting a language model to count.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf


@dataclass
class PageInfo:
    number: int  # 1-based PDF page index
    label: str  # printed page label if the PDF defines one, else ""
    chars: int
    images: int  # embedded raster images
    drawings: int  # vector drawing operations (charts/diagrams are often vector)
    first_text: str
    last_text: str
    last_char: str


def _label(page: pymupdf.Page) -> str:
    try:
        return page.get_label() or ""
    except Exception:  # pages before the first label range, malformed PDFs
        return ""


def _visible(text: str) -> str:
    return "".join(ch for ch in text if not ch.isspace())


def page_info(page: pymupdf.Page) -> PageInfo:
    text = page.get_text()
    compact = _visible(text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    label = _label(page)
    return PageInfo(
        number=page.number + 1,
        label=label,
        chars=len(compact),
        images=len(page.get_images(full=True)),
        drawings=len(page.get_drawings()),
        first_text=lines[0] if lines else "",
        last_text=lines[-1] if lines else "",
        last_char=compact[-1] if compact else "",
    )


def open_pdf(path: Path | str | bytes) -> pymupdf.Document:
    if isinstance(path, bytes):
        return pymupdf.open(stream=path, filetype="pdf")
    return pymupdf.open(str(path))


def summary(doc: pymupdf.Document) -> dict:
    meta = doc.metadata or {}
    return {
        "pages": doc.page_count,
        "title": meta.get("title", ""),
        "author": meta.get("author", ""),
        "has_labels": any(_label(doc[i]) for i in range(min(doc.page_count, 50))),
    }


def page_texts(doc: pymupdf.Document) -> list[tuple[int, str]]:
    return [(p.number + 1, p.get_text()) for p in doc]


def find_label(doc: pymupdf.Document, label: str) -> int | None:
    """Map a printed page label (e.g. '50') to its 1-based PDF page number."""
    for p in doc:
        if _label(p) == label:
            return p.number + 1
    return None


def render_page(doc: pymupdf.Document, number: int, out: Path, zoom: float = 2.0) -> Path:
    """Render a 1-based page to PNG (for a vision model or for you to look at)."""
    pix = doc[number - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    out.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(out))
    return out


def page_png(doc: pymupdf.Document, number: int, zoom: float = 1.5) -> bytes:
    """A 1-based page as PNG bytes (for a vision model)."""
    return doc[number - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")

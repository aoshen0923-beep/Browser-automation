"""Tokenization for the full-text index.

SQLite's built-in FTS5 tokenizers don't segment Chinese, so text is
pre-tokenized: runs of CJK characters become overlapping character bigrams
(a single CJK character stays a unigram) and Latin/digit runs become
lowercase words. Bigrams need no dictionary, so proper nouns, product
names and standard numbers still match.
"""

from __future__ import annotations

import re

_CJK = r"㐀-䶿一-鿿豈-﫿"
_TOKEN_RE = re.compile(rf"[{_CJK}]+|[A-Za-z0-9]+(?:[.\-][A-Za-z0-9]+)*")
_CJK_RE = re.compile(rf"[{_CJK}]")

# Very common function bigrams/words that only add noise to OR queries.
_STOP = {
    "的", "了", "是", "在", "和", "与", "中", "下列", "以下", "哪些", "哪个",
    "请问", "什么", "可以", "其中", "这个", "一个", "如下", "选项", "问题",
    "the", "a", "an", "of", "and", "or", "in", "on", "to", "is", "for",
}


def tokens(text: str) -> list[str]:
    out: list[str] = []
    for run in _TOKEN_RE.findall(text):
        if _CJK_RE.match(run):
            if len(run) == 1:
                out.append(run)
            else:
                out.extend(run[i : i + 2] for i in range(len(run) - 1))
        else:
            word = run.lower()
            out.append(word)
            # Also index the parts of dotted/hyphenated IDs (GB/T 38880-2020).
            parts = re.split(r"[.\-]", word)
            if len(parts) > 1:
                out.extend(p for p in parts if p)
    return out


def index_text(text: str) -> str:
    return " ".join(tokens(text))


def fts_query(text: str, limit: int = 64) -> str:
    """Build an FTS5 OR query from free text; empty string if nothing usable."""
    seen: dict[str, None] = {}
    for tok in tokens(text):
        if tok not in _STOP and tok not in seen:
            seen[tok] = None
        if len(seen) >= limit:
            break
    return " OR ".join(f'"{t}"' for t in seen)


def chunk(text: str, size: int = 700, overlap: int = 120) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) <= size:
        return [text] if text else []
    pieces = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        # Prefer to break at a line or sentence boundary near the end.
        if end < len(text):
            window = text[start + size // 2 : end]
            cut = max(window.rfind("\n"), window.rfind("。"), window.rfind(". "))
            if cut > 0:
                end = start + size // 2 + cut + 1
        pieces.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [p for p in pieces if p]

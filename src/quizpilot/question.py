"""Parse a pasted contest question into stem, type and options."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SINGLE, MULTI, JUDGE = "single", "multi", "judge"

# Option markers such as "A、", "B .", "C．", "D:" or "A)" — PDFs and web pages
# often insert a stray space before the punctuation.
_MARK = re.compile(r"(?<![A-Za-z])([A-D])\s*[、.．:：)）]\s*")
_ANSWER = re.compile(r"正确答案\s*[:：]\s*(\S+)")
_TYPE_HINTS = [
    (MULTI, re.compile(r"多选")),
    (JUDGE, re.compile(r"判断")),
    (SINGLE, re.compile(r"单选")),
]


@dataclass
class Question:
    stem: str
    kind: str
    options: dict[str, str] = field(default_factory=dict)
    expected: str | None = None  # "ABD", "对"/"错" when the paste includes the key

    def render(self) -> str:
        lines = [f"[{self.kind}] {self.stem}"]
        lines += [f"{k}. {v}" for k, v in self.options.items()]
        return "\n".join(lines)


def _find_options(text: str) -> tuple[int, dict[str, str]] | None:
    """Locate A..D markers in order; return (start_of_A, options)."""
    marks = list(_MARK.finditer(text))
    # Try each "A" marker from the last one backwards so an "A" inside the
    # stem doesn't swallow the real options.
    for i in range(len(marks) - 1, -1, -1):
        if marks[i].group(1) != "A":
            continue
        seq = [marks[i]]
        for m in marks[i + 1 :]:
            want = chr(ord(seq[-1].group(1)) + 1)
            if m.group(1) == want:
                seq.append(m)
            if len(seq) == 4:
                break
        if len(seq) >= 2:
            opts = {}
            for j, m in enumerate(seq):
                end = seq[j + 1].start() if j + 1 < len(seq) else len(text)
                opts[m.group(1)] = re.sub(r"\s+", " ", text[m.end() : end]).strip()
            return seq[0].start(), opts
    return None


def normalize_judgement(value: str) -> str | None:
    v = value.strip().rstrip("。.").lower()
    if v in {"对", "正确", "√", "true", "t", "yes", "是"}:
        return "对"
    if v in {"错", "错误", "×", "x", "false", "f", "no", "否"}:
        return "错"
    return None


def parse_question(raw: str, kind: str | None = None) -> Question:
    text = raw.replace("\r\n", "\n").strip()
    expected = None
    m = _ANSWER.search(text)
    if m:
        expected = m.group(1)
        text = text[: m.start()].strip()
    # Drop a trailing "答案解析" block if the answer key was pasted first.
    text = re.split(r"答案解析\s*[:：]", text)[0].strip()

    found = _find_options(text)
    options: dict[str, str] = {}
    stem = text
    if found:
        start, options = found
        stem = text[:start].strip()
    stem = re.sub(r"\s+", " ", stem)
    stem = re.sub(r"^(样题\d*)?\s*[（(][^）)]*题[）)]\s*[:：]?\s*", "", stem)

    if kind is None:
        for k, pat in _TYPE_HINTS:
            if pat.search(raw[:40]):
                kind = k
                break
    if kind is None:
        if not options:
            kind = JUDGE
        elif re.search(r"哪些|哪几|包括|有哪|属于", stem):
            kind = MULTI
        else:
            kind = SINGLE

    if expected is not None:
        if kind == JUDGE:
            expected = normalize_judgement(expected) or expected
        else:
            expected = "".join(sorted(set(re.findall(r"[A-D]", expected.upper()))))
    return Question(stem=stem, kind=kind, options=options, expected=expected)


def split_questions(text: str) -> list[str]:
    """Split a practice file into question blocks separated by lines of ---."""
    blocks = re.split(r"^\s*-{3,}\s*$", text, flags=re.M)
    return [b.strip() for b in blocks if b.strip()]

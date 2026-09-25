"""Pull the sample questions (样题) and their answers out of the prep guide PDF.

The result is a practice file for `quizpilot eval`: question blocks
separated by lines of ---, each ending with its 正确答案 line.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import pdftools

_PAGE_NUM = re.compile(r"^\s*(-\s*\d+\s*-|[IVX]+)\s*$", re.M)
_SAMPLE = re.compile(
    r"样题\s*\d*\s*[（(]([^）)]*题)[）)]\s*[:：]?(.*?)正确答案\s*[:：]\s*([^\n]+)",
    re.S,
)
_MODULE = re.compile(r"知识模块(\d\d)：")


def extract_samples(pdf: Path) -> list[dict]:
    with pdftools.open_pdf(pdf) as doc:
        text = "\n".join(t for _, t in pdftools.page_texts(doc))
    text = _PAGE_NUM.sub("", text)
    samples = []
    # Skip the table of contents: module bodies start after the last TOC entry.
    bodies = _MODULE.split(text)
    for i in range(1, len(bodies), 2):
        module, body = bodies[i], bodies[i + 1]
        for m in _SAMPLE.finditer(body):
            kind, question, answer = m.group(1), m.group(2), m.group(3)
            question = re.sub(r"\n(?!\s*[A-D]\s*[、.．])", "", question).strip()
            answer = re.split(r"\s|答案解析", answer.strip())[0]
            samples.append({"module": module, "kind": kind, "question": question, "answer": answer})
    return samples


def to_practice_text(samples: list[dict]) -> str:
    blocks = [
        f"# 模块{s['module']}（{s['kind']}）\n{s['question']}\n正确答案：{s['answer']}" for s in samples
    ]
    return "\n---\n".join(blocks) + "\n"

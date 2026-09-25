"""Answer a question from the local knowledge base, option by option.

Scoring in the contest punishes wrong answers (-1), so the solver reports
a confidence and whether answering beats skipping in expectation.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .kb import KB, Hit
from .llm import ChatModel
from .question import JUDGE, MULTI, SINGLE, Question, normalize_judgement

# (points for a correct answer, points for a wrong one); skipping scores 0.
ROUNDS: dict[str, tuple[float, float]] = {
    "individual": (2, -1),
    "team": (4, -1),
}


def break_even(round_name: str) -> float:
    gain, loss = ROUNDS[round_name]
    return -loss / (gain - loss)


def worth_answering(confidence: float, round_name: str) -> bool:
    return confidence > break_even(round_name)


SYSTEM = """你是信息素养大赛的答题助手。你会收到一道客观题和从本地知识库检索到的证据片段。
规则：
1. 逐个选项判断真假，优先依据证据片段；证据中没有的内容可以用常识，但要降低置信度。
2. 关于具体网站界面、菜单、筛选项、数量、页码等细节，如果证据中没有，置信度不得超过0.4。
3. 单选题只给一个字母；多选题给出所有正确字母（2-4个）；判断题回答"对"或"错"。
4. answer 绝不能为空：即使没有把握也必须给出最可能的答案，用 confidence 表达把握程度（是否作答由程序根据 confidence 决定）。
5. confidence 是"整个答案完全正确"的概率（0到1），要诚实，不要夸大。
只输出JSON：
{"answer": "AC", "confidence": 0.0, "options": {"A": "true|false|unknown：一句理由"}, "citations": [1], "reason": "一句话"}"""


@dataclass
class Answer:
    answer: str
    confidence: float
    reason: str = ""
    options: dict[str, str] = field(default_factory=dict)
    citations: list[Hit] = field(default_factory=list)
    seconds: float = 0.0
    error: str = ""


def build_prompt(q: Question, hits: list[Hit]) -> str:
    kind = {SINGLE: "单选题", MULTI: "多选题（2-4个正确答案）", JUDGE: "判断题"}[q.kind]
    parts = [f"题型：{kind}", f"题干：{q.stem}"]
    parts += [f"{k}、{v}" for k, v in q.options.items()]
    parts.append("\n证据片段：" if hits else "\n证据片段：（本地知识库中没有找到相关内容）")
    for i, h in enumerate(hits, 1):
        where = f" 第{h.page}页" if h.page else ""
        parts.append(f"[{i}] 来源：{h.title}{where}\n{h.text}")
    return "\n".join(parts)


def normalize_answer(q: Question, raw: object) -> str:
    text = str(raw or "").strip()
    if q.kind == JUDGE:
        return normalize_judgement(text) or ""
    letters = "".join(sorted(set(re.findall(r"[A-D]", text.upper())) & set(q.options)))
    if q.kind == SINGLE and len(letters) != 1:
        return ""
    return letters


def solve(q: Question, kb: KB, model: ChatModel, top_k: int = 8) -> Answer:
    start = time.monotonic()
    query = q.stem + " " + " ".join(q.options.values())
    hits = kb.search(query, k=top_k)
    try:
        reply = model.chat_json(SYSTEM, build_prompt(q, hits))
    except Exception as e:  # network/API errors must not crash the answer loop
        return Answer("", 0.0, error=str(e), seconds=time.monotonic() - start)

    answer = normalize_answer(q, reply.get("answer"))
    try:
        confidence = max(0.0, min(1.0, float(reply.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    if not answer:
        confidence = 0.0
    cited = []
    for n in reply.get("citations") or []:
        try:
            idx = int(n) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(hits) and hits[idx] not in cited:
            cited.append(hits[idx])
    options = reply.get("options") if isinstance(reply.get("options"), dict) else {}
    return Answer(
        answer=answer,
        confidence=confidence,
        reason=str(reply.get("reason", "")),
        options={str(k): str(v) for k, v in options.items()},
        citations=cited,
        seconds=time.monotonic() - start,
    )

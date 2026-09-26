"""What quizpilot learns from graded questions and from trouble with sites.

- A graded question's correct answer is remembered, so the same question
  (even with its options shuffled) is answered at once next time.
- A correct answer's research steps become a verified approach ("做法")
  that similar questions start from.
- A wrong answer is reviewed by the model into a short lesson ("教训")
  that similar questions are shown, and its approach is thrown away.
- Sites that fail to open, load very slowly or show verification pages
  get a note the agent sees before using them again.

All of it lives in the knowledge base, so it travels to teammates with
export / merge like everything else.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from urllib.parse import urlparse

from .kb import KB
from .question import JUDGE, Question

MODULE = "memory"

LESSON_SYSTEM = """你在复盘一道信息检索竞赛的客观题：当时的答案错了。根据题目、当时的操作步骤和正确答案，
总结一条下次能直接照做的经验教训。只输出JSON：
{"lesson":"不超过120字：错在哪里（比如看错了地方、没核对全部选项、用了不可靠的网站、没翻页/没打开详情），下次类似题应该去哪个网站、怎么查、核对什么",
 "sites":{"域名":"关于这个网站的一条具体经验（可省略）"}}"""


def _key(q: Question) -> str:
    return hashlib.sha1(re.sub(r"[\W_]+", "", q.stem).encode("utf-8")).hexdigest()[:16]


def recipe_source(q: Question) -> str:
    # Same scheme recipes have always used, so older ones are found too.
    return "recipe:" + hashlib.sha1(q.stem.encode("utf-8")).hexdigest()[:16]


def _host(url_or_host: str) -> str:
    host = urlparse(url_or_host).netloc if "//" in url_or_host else url_or_host
    host = host.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


# --- remembered answers --------------------------------------------------------------

def save_answer(kb: KB, q: Question, correct: str) -> None:
    lines = [f"题目：{q.stem}"] + [f"{k}、{v}" for k, v in q.options.items()]
    if q.kind == JUDGE:
        texts = [correct]
    else:
        texts = [q.options[c] for c in correct if c in q.options]
    lines.append(f"正确答案：{correct}（{'；'.join(texts)}）")
    lines.append("正确选项：" + json.dumps(texts, ensure_ascii=False))
    lines.append("全部选项：" + json.dumps(list(q.options.values()), ensure_ascii=False))
    kb.add_document(f"qa:{_key(q)}", [(None, "\n".join(lines))], title=f"做过的题：{q.stem[:40]}",
                    kind="qa", module=MODULE)


def known_answer(kb: KB | None, q: Question) -> str:
    """The graded answer to this very question, mapped onto its current option letters."""
    if kb is None:
        return ""
    text = kb.get_text(f"qa:{_key(q)}")
    m = re.search(r"^正确选项：(.*)$", text or "", re.M)
    if not m:
        return ""
    every = re.search(r"^全部选项：(.*)$", text, re.M)
    try:
        texts = json.loads(m.group(1))
        every = json.loads(every.group(1)) if every else None
    except ValueError:
        return ""
    squash = lambda t: re.sub(r"\s+", "", t)  # noqa: E731
    if every is not None and sorted(map(squash, every)) != sorted(map(squash, q.options.values())):
        return ""  # a different set of options can have a different answer
    if q.kind == JUDGE:
        return texts[0] if texts and texts[0] in ("对", "错") else ""
    by_text = {re.sub(r"\s+", "", v): k for k, v in q.options.items()}
    letters = [by_text.get(re.sub(r"\s+", "", t)) for t in texts]
    if not letters or None in letters:
        return ""  # the options changed: not the same question after all
    return "".join(sorted(letters))


# --- approaches ------------------------------------------------------------------------

def save_recipe(kb: KB, q: Question, steps: list, final: dict, *, verified: bool = False) -> bool:
    """Remember how a question was solved (steps = agent Step objects)."""
    if not verified and "已经核实答对" in (kb.get_text(recipe_source(q)) or ""):
        return False  # never replace a verified approach with an unchecked one
    useful = [s for s in steps
              if s.action.get("action") not in ("（无效输出）", "answer待核实", "用户手动操作")
              and not s.result.startswith(("失败", "重复操作", "被用户打断"))]
    if not useful:
        return False
    lines = [f"题目：{q.stem}"]
    if verified:
        lines.append("（这个做法已经核实答对了）")
    for i, s in enumerate(useful, 1):
        a = {k: v for k, v in s.action.items() if k != "ref"}
        lines.append(f"{i}. {json.dumps(a, ensure_ascii=False)}")
    if final.get("evidence"):
        lines.append(f"依据：{str(final['evidence'])[:200]}")
    title = ("做法（已核实）：" if verified else "做法：") + q.stem[:40]
    kb.add_document(recipe_source(q), [(None, "\n".join(lines))], title=title, kind="recipe", module="recipe")
    return True


# --- lessons from mistakes -----------------------------------------------------------

def _steps_text(steps: list, limit: int = 25) -> str:
    out = []
    for i, s in enumerate(steps[:limit], 1):
        a = {k: v for k, v in s.action.items() if k != "ref"}
        result = re.sub(r"\s+", " ", s.result)[:160]
        out.append(f"{i}. {json.dumps(a, ensure_ascii=False)[:200]} → {result}")
    return "\n".join(out) or "（没有上网查找，只凭本地知识库作答）"


def reflect(model, q: Question, given: str, correct: str, steps: list, evidence: str = "") -> tuple[str, dict]:
    """Ask the model what went wrong; falls back to a plain note if it can't answer."""
    options = "\n".join(f"{k}、{v}" for k, v in q.options.items())
    user = (f"题目：{q.stem}\n{options}\n当时的答案：{given or '（没答）'}\n正确答案：{correct}\n"
            f"当时引用的证据：{evidence or '（无）'}\n当时的操作步骤：\n{_steps_text(steps)}")
    try:
        reply = model.chat_json(LESSON_SYSTEM, user, 500)
        lesson = str(reply.get("lesson", "")).strip()
        sites = reply.get("sites") if isinstance(reply.get("sites"), dict) else {}
    except Exception:
        lesson, sites = "", {}
    if not lesson:
        visited = [s.action.get("url") for s in steps if s.action.get("url")]
        lesson = "当时答错了，要到官方网站找到原文逐个核对选项" + (f"；当时看过：{'、'.join(visited[:3])}" if visited else "")
    return lesson[:300], {str(k): str(v)[:150] for k, v in sites.items() if str(v).strip()}


def lessons_for(kb: KB | None, text: str, k: int = 3) -> list[str]:
    if kb is None:
        return []
    try:
        return [h.text for h in kb.search(text, k=k, kind="lesson") if h.score > 3]
    except Exception:
        return []


def learn(kb: KB, model, q: Question, given: str, correct: str, steps: list | None = None,
          final: dict | None = None) -> dict:
    """Record a graded question. Returns {"right", "correct", "note"} for display."""
    from .solver import normalize_answer

    steps = steps or []
    correct = normalize_answer(q, correct)
    if not correct:
        raise ValueError("正确答案看不懂：单选填一个字母，多选填几个字母，判断题填 对 或 错")
    save_answer(kb, q, correct)
    if given == correct:
        kept = save_recipe(kb, q, steps, final or {}, verified=True)
        return {"right": True, "correct": correct,
                "note": "已记住这道题的答案" + ("和做法，类似的题会照着做" if kept else "")}
    kb.remove(recipe_source(q))  # the approach led to a wrong answer: don't reuse it
    lesson, sites = reflect(model, q, given, correct, steps, str((final or {}).get("evidence", "")))
    options = "；".join(f"{k}、{v}" for k, v in q.options.items())
    text = f"题目：{q.stem}\n选项：{options}\n正确答案：{correct}（当时答了 {given or '没答'}）\n教训：{lesson}"
    kb.add_document(f"lesson:{_key(q)}", [(None, text)], title=f"教训：{q.stem[:40]}", kind="lesson", module=MODULE)
    for host, note in sites.items():
        add_site_note(kb, host, f"lesson-{_key(q)}", note)
    return {"right": False, "correct": correct, "note": f"已记下教训：{lesson}"}


# --- site notes ------------------------------------------------------------------------

def add_site_note(kb: KB | None, url_or_host: str, key: str, note: str, keep: int = 8) -> None:
    """One note per (site, key); a newer note with the same key replaces the older one."""
    host = _host(url_or_host)
    if kb is None or not host:
        return
    source = f"site:{host}"
    # dict.fromkeys: long notes are stored in overlapping chunks, so lines can repeat
    lines = list(dict.fromkeys(ln for ln in (kb.get_text(source) or "").splitlines() if ln.startswith("【")))
    lines = [ln for ln in lines if not ln.startswith(f"【{key}】")]
    lines.append(f"【{key}】{time.strftime('%Y-%m-%d')} {note}")
    lines = lines[-keep:]
    kb.add_document(source, [(None, "\n".join(lines))], title=f"网站经验：{host}", kind="sitenote", module=MODULE)


def site_notes(kb: KB | None, url_or_host: str) -> list[str]:
    host = _host(url_or_host)
    if kb is None or not host:
        return []
    text = kb.get_text(f"site:{host}") or ""
    return [re.sub(r"^【[^】]*】", "", ln) for ln in dict.fromkeys(text.splitlines()) if ln.startswith("【")]

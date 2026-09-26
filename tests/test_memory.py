import asyncio
import json
import threading
import time

from quizpilot import memory
from quizpilot.agent import Agent, Step
from quizpilot.browser import Browser
from quizpilot.config import Config
from quizpilot.kb import KB
from quizpilot.question import parse_question
from quizpilot.solver import solve
from quizpilot.ui import App

from test_agent import QUESTION, std_site  # noqa: F401  (fixture)
from test_browser import chrome  # noqa: F401
from test_ui import call


def filler(kb):
    """A realistic knowledge base holds many documents (search scores need them)."""
    for i in range(30):
        kb.add_document(f"filler{i}", [(None, f"无关资料{i}：专利 数据库 标准 检索 说明")], kind="page", module="11")

SHUFFLED = "《儿童口罩技术规范》（GB/T 38880-2020）以下哪一位不是该标准的起草人？（）\nA、许伟民\nB、李桂梅\nC、高尚荣\nD、李建全"


class Reviewer:
    def __init__(self):
        self.calls = []

    def chat_json(self, system, user, max_tokens=700):
        self.calls.append((system, user))
        if "复盘" in system:
            return {"lesson": "起草人要看标准全文PDF的前言，不要看题库网站",
                    "sites": {"itihey.com": "答案经常错，只能当线索"}}
        raise AssertionError("the solver should answer from memory without asking the model")


def test_remembered_answer_survives_shuffled_options():
    q = parse_question(QUESTION, "single")
    with KB(":memory:") as kb:
        assert memory.known_answer(kb, q) == ""
        memory.save_answer(kb, q, "C")
        assert memory.known_answer(kb, q) == "C"
        assert memory.known_answer(kb, parse_question(SHUFFLED, "single")) == "A"  # 许伟民 moved to A
        other = parse_question(QUESTION.replace("李建全", "王五"), "single")
        assert memory.known_answer(kb, other) == ""  # different options: not the same question
        ans = solve(parse_question(SHUFFLED, "single"), kb, Reviewer())
        assert ans.answer == "A" and ans.confidence == 0.99 and ans.memory
        judge = parse_question("CNKI 可以按基金检索。", "judge")
        memory.save_answer(kb, judge, "对")
        assert memory.known_answer(kb, judge) == "对"


def test_right_answers_keep_the_approach_wrong_ones_become_lessons():
    q = parse_question(QUESTION, "single")
    steps = [Step({"action": "goto", "url": "https://std.samr.gov.cn"}, "已打开"),
             Step({"action": "pdf", "page": 2}, "本标准主要起草人：高尚荣、李桂梅、李建全")]
    model = Reviewer()
    with KB(":memory:") as kb:
        filler(kb)
        out = memory.learn(kb, model, q, "C", "c", steps)
        assert out["right"] and "做法" in out["note"]
        recipe = kb.get_text(memory.recipe_source(q))
        assert "已经核实答对" in recipe and "std.samr.gov.cn" in recipe
        # An unchecked approach never replaces a verified one.
        assert not memory.save_recipe(kb, q, steps[:1], {})
        assert "已经核实答对" in kb.get_text(memory.recipe_source(q))

        out = memory.learn(kb, model, q, "B", "C", [Step({"action": "goto", "url": "https://www.itihey.com/x"}, "已打开")])
        assert not out["right"] and "前言" in out["note"]
        assert kb.get_text(memory.recipe_source(q)) is None  # the approach that failed is gone
        assert "当时答了 B" in model.calls[-1][1] or "当时的答案：B" in model.calls[-1][1]
        lessons = memory.lessons_for(kb, "某标准的起草人是谁 儿童口罩")
        assert lessons and "教训：起草人要看标准全文PDF的前言" in lessons[0]
        assert memory.site_notes(kb, "https://itihey.com/abc") == [time.strftime("%Y-%m-%d") + " 答案经常错，只能当线索"]
        info = kb.overview()
        assert info["answers"] == 1 and info["lessons"] == 1 and info["sitenotes"] == 1
        assert set(info["modules"]) == {"11"}


def test_site_notes_replace_by_kind_and_stay_short():
    with KB(":memory:") as kb:
        memory.add_site_note(kb, "https://www.cnki.net/kns", "slow", "打开很慢")
        memory.add_site_note(kb, "cnki.net", "slow", "又很慢")
        memory.add_site_note(kb, "https://cnki.net", "captcha", "出现过验证")
        notes = memory.site_notes(kb, "http://www.cnki.net/")
        assert [n.split(" ", 1)[1] for n in notes] == ["又很慢", "出现过验证"]
        for i in range(12):
            memory.add_site_note(kb, "cnki.net", f"k{i}", "说明" * 30)
        assert len(memory.site_notes(kb, "cnki.net")) == 8


def test_agent_is_told_lessons_and_site_notes(std_site, chrome):  # noqa: F811
    prompts = []

    class Model:
        def chat_json(self, system, user, max_tokens=700):
            prompts.append(user)
            if "空白页" in user:
                return {"action": "goto", "url": std_site + "/results.html"}
            return {"action": "answer", "answer": "C", "confidence": 0.5}

    async def run(kb):
        async with Browser(chrome) as browser:
            agent = Agent(browser, Model(), [], kb, log=lambda *_: None)
            return await agent.run(parse_question(QUESTION, "single"), budget=60, close=True)

    with KB(":memory:") as kb:
        filler(kb)
        q = parse_question(QUESTION, "single")
        memory.learn(kb, Reviewer(), q, "A", "C", [])
        memory.add_site_note(kb, std_site, "slow", "打开很慢")
        asyncio.run(run(kb))
    assert "以前做错过的相似题的教训" in prompts[0] and "起草人要看标准全文PDF的前言" in prompts[0]
    assert "这道题以前做过并核实过：正确答案是 C" in prompts[0]
    assert "关于这个网站以前的经验" in prompts[1] and "打开很慢" in prompts[1]


def test_grading_on_the_answering_page(std_site, chrome, tmp_path):  # noqa: F811
    class Model:
        def __init__(self):
            self.live = 0

        def chat_json(self, system, user, max_tokens=700):
            if "复盘" in system:
                return {"lesson": "要打开标准全文核对起草人名单"}
            if "网站目录" not in system:
                return {"answer": "A", "confidence": 0.3}
            self.live += 1
            if "空白页" in user:
                return {"action": "goto", "url": std_site + "/results.html"}
            return {"action": "answer", "answer": "B", "confidence": 0.8, "evidence": "GB/T 38880-2020 儿童口罩技术规范"}

    model = Model()
    cfg = Config(root=tmp_path)
    cfg.browser.cdp_url = chrome
    with KB(":memory:") as kb:
        app = App(cfg, kb, model, [])
        t = threading.Thread(target=app.serve, kwargs={"port": 0, "open_browser": False}, daemon=True)
        t.start()
        while not app.url:
            time.sleep(0.1)
        base = app.url.rstrip("/")

        def ask(text, live=True):
            job = call(base + "/api/ask", {"question": text, "round": "team", "live": live, "budget": 60})
            for _ in range(200):
                full = call(f"{base}/api/jobs/{job['id']}")
                if full["status"] == "done" and not (full["feedback"] or {}).get("status"):
                    return full
                time.sleep(0.2)
            raise AssertionError(full)

        first = ask(QUESTION)
        assert first["final"]["answer"] == "B" and first["feedback"] is None
        call(f"{base}/api/jobs/{first['id']}/grade", {"correct": "C"})  # 答错了，正确答案 C
        for _ in range(50):
            fb = call(f"{base}/api/jobs/{first['id']}")["feedback"]
            if fb and "right" in fb:
                break
            time.sleep(0.2)
        assert fb["right"] is False and fb["correct"] == "C" and "标准全文" in fb["note"]
        # The same question again: answered from memory at once, no browsing.
        lives = model.live
        again = ask(SHUFFLED)
        assert again["quick"]["answer"] == "A" and again["final"] is None and model.live == lives
        assert any("以前做过" in line for line in again["log"])
        # A practice question pasted with its key grades itself.
        practice = "中国国家标准的代号是？\nA、GB\nB、ISO\nC、ANSI\nD、JIS\n正确答案：A"
        graded = ask(practice, live=False)
        assert graded["feedback"]["right"] is True and graded["expected"] == "A"
        app.loop.call_soon_threadsafe(app.loop.stop)
        t.join(10)

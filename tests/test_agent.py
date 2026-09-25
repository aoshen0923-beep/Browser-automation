import asyncio
import functools
import http.server
import re
import threading

import pymupdf
import pytest

from quizpilot.agent import Agent, site_directory
from quizpilot.browser import Browser
from quizpilot.kb import KB
from quizpilot.question import parse_question

from test_browser import chrome  # noqa: F401  (fixture: headless Chromium over CDP)


class ScriptedModel:
    """Acts like the LLM: reads the observation and picks the next action."""

    def __init__(self):
        self.prompts = []

    def chat_json(self, system, user, max_tokens=700):
        self.prompts.append(user)
        assert "网站目录" in system
        if "时间到了" in user or "主要起草人" in user:
            return {"action": "answer", "answer": "C", "confidence": 0.95, "reason": "起草人名单里没有许伟民",
                    "evidence": "本标准主要起草人：高尚荣、李桂梅、李建全"}
        if "空白页" in user:
            return {"action": "goto", "url": SITE["url"] + "/index.html"}
        m = re.search(r"\[(\d+)\] a -> std\.pdf", user)
        if m:
            return {"action": "click", "ref": int(m.group(1))}
        if "共2页" in user:
            return {"action": "pdf", "page": 2}
        m = re.search(r"\[(\d+)\] input type=text", user)
        if m and "已输入" not in user:
            return {"action": "type", "ref": int(m.group(1)), "text": "GB/T 38880", "enter": True}
        return {"action": "find", "text": "起草人"}


SITE = {}


@pytest.fixture
def std_site(tmp_path):
    (tmp_path / "index.html").write_text(
        '<meta charset="utf-8"><title>标准检索</title>'
        '<form action="results.html"><input type="text" name="q" placeholder="标准号"><button>检索</button></form>',
        encoding="utf-8",
    )
    (tmp_path / "results.html").write_text(
        '<meta charset="utf-8"><title>结果</title><p>GB/T 38880-2020 儿童口罩技术规范</p>'
        '<a href="std.pdf">查看全文</a>',
        encoding="utf-8",
    )
    # A search form inside an iframe (common on government/database sites).
    (tmp_path / "framed.html").write_text(
        '<meta charset="utf-8"><title>外框</title><p>欢迎</p><iframe src="inner.html" width="600" height="300"></iframe>',
        encoding="utf-8",
    )
    (tmp_path / "inner.html").write_text(
        '<meta charset="utf-8"><form action="results.html" target="_top"><input type="text" name="q" placeholder="标准号">'
        "<button>检索</button></form>",
        encoding="utf-8",
    )
    # 150 navigation links before the one content link that matters.
    nav = "".join(f'<a href="n{i}.html">栏目{i}</a>' for i in range(150))
    (tmp_path / "busy.html").write_text(
        f'<meta charset="utf-8"><nav>{nav}</nav><main><a href="std.pdf">GB/T 38880-2020 全文</a></main>',
        encoding="utf-8",
    )
    # An icon-only button (no text) that only vision can find, at a known spot.
    (tmp_path / "icon.html").write_text(
        '<meta charset="utf-8"><title>图标</title><div id="out">点击放大镜查看</div>'
        '<div id="lens" onclick="document.getElementById(\'out\').innerText=\'本标准主要起草人：高尚荣、李桂梅、李建全\'"'
        ' style="position:absolute;left:100px;top:200px;width:40px;height:40px;background:#333;border-radius:20px"></div>',
        encoding="utf-8",
    )
    # A verification page that "gets solved" 1.5 s after loading.
    (tmp_path / "captcha.html").write_text(
        '<meta charset="utf-8"><title>安全验证</title><body>请完成安全验证</body>'
        "<script>setTimeout(() => { document.body.innerText = '检索结果 '.repeat(200); }, 1500)</script>",
        encoding="utf-8",
    )
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "GB/T 38880-2020")
    page = doc.new_page()
    page.insert_font(fontname="china", fontbuffer=pymupdf.Font("china-s").buffer)
    page.insert_text((72, 72), "本标准主要起草人：高尚荣、李桂梅、李建全", fontname="china")
    doc.save(str(tmp_path / "std.pdf"))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    SITE["url"] = f"http://127.0.0.1:{server.server_address[1]}"
    yield SITE["url"]
    server.shutdown()


QUESTION = "《儿童口罩技术规范》（GB/T 38880-2020）以下哪一位不是该标准的起草人？（）\nA、高尚荣\nB、李桂梅\nC、许伟民\nD、李建全"


def test_agent_researches_like_a_person(std_site, chrome):  # noqa: F811
    model = ScriptedModel()
    sites = [{"module": "11", "title": "标准", "urls": [std_site]}]
    logs = []

    async def run():
        with KB(":memory:") as kb:
            async with Browser(chrome) as browser:
                agent = Agent(browser, model, sites, kb, log=logs.append)
                result = await agent.run(parse_question(QUESTION, "single"), budget=60, close=True)
            return result, kb.search("李桂梅 起草人")

    result, hits = asyncio.run(run())
    assert result.answer.answer == "C"
    assert result.answer.confidence == 0.95
    kinds = [s.action["action"] for s in result.steps]
    assert kinds[:3] == ["goto", "type", "click"], (kinds, [s.result for s in result.steps])
    assert "pdf" in kinds or "共2页" in result.steps[2].result
    assert any("主要起草人" in s.result for s in result.steps)
    # The PDF it read was saved for next time.
    assert hits and hits[0].source.endswith("std.pdf")


def test_forced_answer_when_out_of_steps(std_site, chrome):  # noqa: F811
    class Wanderer:
        def chat_json(self, system, user, max_tokens=700):
            if "时间到了" in user:
                return {"action": "answer", "answer": "b", "confidence": 0.3}
            return {"action": "find", "text": "nothing"}

    async def run():
        async with Browser(chrome) as browser:
            agent = Agent(browser, Wanderer(), [], None, log=lambda *_: None)
            return await agent.run(parse_question(QUESTION, "single"), budget=60, max_steps=3, close=True)

    result = asyncio.run(run())
    assert result.answer.answer == "B"
    assert len(result.steps) == 2


def test_site_directory_lists_urls():
    text = site_directory([{"module": "11", "title": "标准", "urls": ["https://a", "https://b"]},
                           {"module": "40", "title": "截图", "urls": []}])
    assert text == "11 标准: https://a https://b"


def test_model_timeouts_do_not_crash_the_run(std_site, chrome):  # noqa: F811
    from quizpilot.llm import LLMError

    class Flaky:
        calls = 0

        def chat_json(self, system, user, max_tokens=700):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise LLMError("request failed: The read operation timed out")
            if "主要起草人" in user:
                return {"action": "answer", "answer": "C", "confidence": 0.9}
            if "共2页" in user:
                return {"action": "pdf", "page": 2}
            return {"action": "goto", "url": std_site + "/std.pdf"}

    class Dead:
        def chat_json(self, system, user, max_tokens=700):
            raise LLMError("request failed: The read operation timed out")

    async def run(model):
        async with Browser(chrome) as browser:
            agent = Agent(browser, model, [], None, log=lambda *_: None)
            return await agent.run(parse_question(QUESTION, "single"), budget=60, close=True)

    result = asyncio.run(run(Flaky()))
    assert result.answer.answer == "C", [(s.action, s.result[:80]) for s in result.steps]
    dead = asyncio.run(run(Dead()))
    assert dead.answer.answer == "" and dead.answer.confidence == 0.0


def test_suggest_sites_routes_to_the_right_module():
    from quizpilot.agent import suggest_sites
    from quizpilot.cli import load_sites

    sites = load_sites()

    def top(text):
        return suggest_sites(text, sites)[0]["module"]

    assert top("国家标准《儿童口罩技术规范》（GB/T 38880-2020）的起草人") == "11"
    assert top("小米公司的发明专利，通过国家知识产权局专利检索系统") == "12"
    assert top("在arXiv中找到arXiv identifier为2108.09800的文献") == "24"
    assert top("星河互联集团有限公司被列为失信被执行人") == "10"
    assert top("在剑桥数据库中找到名为《Big Data and Global Trade Law》的电子书") == "19"


def test_repeated_goto_is_skipped(std_site, chrome):  # noqa: F811
    class Looper:
        n = 0

        def chat_json(self, system, user, max_tokens=700):
            Looper.n += 1
            if Looper.n >= 4:
                return {"action": "answer", "answer": "C", "confidence": 0.5}
            return {"action": "goto", "url": std_site + "/index.html"}

    async def run():
        async with Browser(chrome) as browser:
            agent = Agent(browser, Looper(), [], None, log=lambda *_: None)
            return await agent.run(parse_question(QUESTION, "single"), budget=60, close=True)

    result = asyncio.run(run())
    assert [s.result.startswith("重复操作") for s in result.steps] == [False, True, True]


def test_captcha_wait_is_reported_and_research_continues(std_site, chrome, tmp_path):  # noqa: F811
    class OneStep:
        n = 0

        def chat_json(self, system, user, max_tokens=700):
            OneStep.n += 1
            if OneStep.n == 1:
                return {"action": "goto", "url": std_site + "/captcha.html"}
            return {"action": "answer", "answer": "C", "confidence": 0.5}

    logs = []

    async def run():
        async with Browser(chrome) as browser:
            agent = Agent(browser, OneStep(), [], None, log=logs.append)
            return await agent.run(parse_question(QUESTION, "single"), budget=60, close=True)

    result = asyncio.run(run())
    assert result.answer.answer == "C"
    assert any("需要人机验证" in line for line in logs), logs
    assert any("验证已通过" in line for line in logs), logs


def test_batched_actions_in_an_iframe_and_recipes(std_site, chrome):  # noqa: F811
    """type + click in one step, inside an iframe; the solved approach is reused next time."""
    prompts = []

    class Batcher:
        def chat_json(self, system, user, max_tokens=700):
            prompts.append(user)
            if "主要起草人" in user:
                return {"memory": "找到起草人名单", "actions": [{"action": "answer", "answer": "C", "confidence": 0.9,
                                                               "evidence": "本标准主要起草人：高尚荣、李桂梅、李建全"}]}
            if "共2页" in user:
                return {"actions": [{"action": "pdf", "page": 2}]}
            m = re.search(r"\[(\d+)\] a -> std\.pdf", user)
            if m:
                return {"actions": [{"action": "click", "ref": int(m.group(1))}]}
            m = re.search(r"\[(1-\d+)\] input type=text", user)
            b = re.search(r"\[(1-\d+)\] button", user)
            if m and b:
                return {"memory": "在框架里的表单检索", "actions": [
                    {"action": "type", "ref": m.group(1), "text": "GB/T 38880"},
                    {"action": "click", "ref": b.group(1)},
                    {"action": "goto", "url": "https://should-not-run.example"}]}
            return {"actions": [{"action": "goto", "url": std_site + "/framed.html"}]}

    logs = []

    async def run(kb):
        async with Browser(chrome) as browser:
            agent = Agent(browser, Batcher(), [], kb, log=logs.append)
            return await agent.run(parse_question(QUESTION, "single"), budget=60, close=True)

    with KB(":memory:") as kb:
        result = asyncio.run(run(kb))
        kinds = [s.action["action"] for s in result.steps]
        assert result.answer.answer == "C", [(s.action, s.result[:60]) for s in result.steps]
        assert kinds[:3] == ["goto", "type", "click"], kinds  # type+click ran in one step
        assert "goto" not in kinds[3:] or all("should-not-run" not in str(s.action) for s in result.steps)
        assert any("[2a]" in line for line in logs) and any("[2b]" in line for line in logs)
        assert "你上一步记下的要点：在框架里的表单检索" in "\n".join(prompts)
        # The approach was saved as a recipe and is offered for the same question next time.
        recipes = kb.search(QUESTION, kind="recipe")
        assert recipes and "framed.html" in recipes[0].text
        prompts.clear()
        asyncio.run(run(kb))
        assert "以前答对过的相似题的做法" in prompts[0]


def test_content_links_survive_busy_navigation(std_site, chrome):  # noqa: F811
    class Reader:
        def chat_json(self, system, user, max_tokens=700):
            if "空白页" in user:
                return {"action": "goto", "url": std_site + "/busy.html"}
            assert "a -> std.pdf" in user, "content link was cut off by navigation links"
            assert "导航/页脚元素未列出" in user
            return {"action": "answer", "answer": "C", "confidence": 0.5}

    async def run():
        async with Browser(chrome) as browser:
            agent = Agent(browser, Reader(), [], None, log=lambda *_: None)
            return await agent.run(parse_question(QUESTION, "single"), budget=60, close=True)

    assert asyncio.run(run()).answer.answer == "C"


def test_vision_finds_an_icon_button_and_reads_pdf_pages(std_site, chrome):  # noqa: F811
    images_seen = []

    class Seer:
        def chat_json(self, system, user, max_tokens=700, images=None):
            if images:
                images_seen.append(images[0][:4])
                if "有几张图" in user:
                    return {"answer": "0张图"}
                return {"answer": "左上方有一个圆形放大镜图标", "x": 120, "y": 220}
            if "主要起草人" in user and "0张图" in user:
                return {"actions": [{"action": "answer", "answer": "C", "confidence": 0.9}]}
            if "主要起草人" in user:
                return {"actions": [{"action": "pdf", "url": std_site + "/std.pdf", "page": 2, "look": "这一页有几张图"}]}
            if "x=120, y=220" in user:
                return {"actions": [{"action": "click_xy", "x": 120, "y": 220}]}
            if "空白页" in user:
                return {"actions": [{"action": "goto", "url": std_site + "/icon.html"}]}
            return {"actions": [{"action": "look", "question": "放大镜图标在哪里"}]}

    async def run():
        async with Browser(chrome) as browser:
            agent = Agent(browser, Seer(), [], None, log=lambda *_: None)
            return await agent.run(parse_question(QUESTION, "single"), budget=60, close=True)

    result = asyncio.run(run())
    kinds = [s.action["action"] for s in result.steps]
    assert result.answer.answer == "C", [(s.action, s.result[:80]) for s in result.steps]
    assert kinds == ["goto", "look", "click_xy", "pdf"], kinds
    assert images_seen[0] == b"\xff\xd8\xff\xe0" or images_seen[0][:2] == b"\xff\xd8"  # JPEG screenshot
    assert images_seen[1] == b"\x89PNG"  # rendered PDF page


def test_vision_switches_off_when_the_model_rejects_images(std_site, chrome):  # noqa: F811
    from quizpilot.llm import VisionUnsupported

    class TextOnly:
        def chat_json(self, system, user, max_tokens=700, images=None):
            if images:
                raise VisionUnsupported("unknown variant image_url")
            if "视觉不可用" in user:
                return {"actions": [{"action": "answer", "answer": "C", "confidence": 0.4}]}
            if "空白页" in user:
                return {"actions": [{"action": "goto", "url": std_site + "/icon.html"}]}
            return {"actions": [{"action": "look", "question": "图标在哪"}]}

    model = TextOnly()

    async def run():
        async with Browser(chrome) as browser:
            agent = Agent(browser, model, [], None, log=lambda *_: None)
            result = await agent.run(parse_question(QUESTION, "single"), budget=60, close=True)
            second = Agent(browser, model, [], None, log=lambda *_: None)
            return result, second.vision

    result, second_vision = asyncio.run(run())
    assert result.answer.answer == "C"
    assert model.vision_unsupported is True and second_vision is False


def test_answers_from_question_bank_sites_are_capped():
    from quizpilot.agent import unreliable

    assert unreliable("https://itihey.com/question/v1/165994058")
    assert unreliable("https://zhidao.baidu.com/question/1.html")
    assert not unreliable("https://openstd.samr.gov.cn/bzgk/gb/newGbInfo?hcno=1")
    assert not unreliable("")

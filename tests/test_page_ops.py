"""Page operations on a realistic results site: dialogs, paging, scrolling, dropdowns."""

import asyncio
import functools
import http.server
import re
import threading
from pathlib import Path

import pytest

from quizpilot.agent import Agent, evidence_found
from quizpilot.browser import Browser
from quizpilot.question import parse_question

from test_agent import std_site  # noqa: F401  (fixture)
from test_browser import chrome  # noqa: F401  (fixture: headless Chromium over CDP)

SITE_DIR = Path(__file__).parent / "sites" / "library"


@pytest.fixture
def library():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE_DIR))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def ref_in_line(obs: str, line_has: str, element: str) -> str:
    """The number of `element` (e.g. '<a>详情') on the line that contains line_has."""
    for line in obs.splitlines():
        if line_has in line:
            m = re.search(r"\[(\d+)\]" + re.escape(element), line)
            if m:
                return m.group(1)
    raise AssertionError(f"no {element} next to {line_has} in:\n{obs}")


def test_dialogs_paging_scrolling_and_dropdowns(library, chrome):  # noqa: F811
    logs = []

    async def run():
        async with Browser(chrome) as browser:
            agent = Agent(browser, None, [], None, log=logs.append)
            agent.page = await agent._open_tab()
            assert await agent.act({"action": "goto", "url": library + "/results.html"}) == "已打开"
            obs = await agent.observe()
            # The cookie dialog sits last in the page but is shown first.
            assert obs.index("【页面上弹出的对话框】") < obs.index("口罩相关研究第1篇")
            # 30 menu links: 15 shown, the rest moved out of the way.
            assert "另有15个菜单/导航链接" in obs
            assert (await agent.act({"action": "click", "ref": ref_in_line(obs, "同意", "<button>同意")})).startswith("已点击")
            obs = await agent.observe()
            assert "对话框" not in obs
            # A button that does nothing is reported, so the model tries something else.
            assert "没有任何变化" in await agent.act({"action": "click", "ref": ref_in_line(obs, "收藏", "<button>收藏")})
            # Next page: results arrive 1.2 s after the click, behind "正在加载…".
            assert await agent.act({"action": "next_page"}) == "已翻到下一页"
            obs = await agent.observe()
            assert "口罩相关研究第11篇" in obs and "正在加载" not in obs
            # Links are read in context: the 详情 on the target's own line.
            detail = ref_in_line(obs, "基于深度学习的儿童口罩佩戴检测研究", "<a>详情")
            await agent.act({"action": "click", "ref": detail})
            assert agent.page.url.endswith("detail.html?id=17")
            # The funding line loads only when the inner panel is scrolled to its end.
            obs = await agent.observe()
            assert "基金项目" not in obs
            results = []
            for _ in range(8):
                results.append(await agent.act({"action": "scroll", "direction": "down"}))
                obs = await agent.observe()
                if "基金项目" in obs:
                    break
            assert "61876543" in obs, results
            assert "屏" in results[0]
            # A script-driven dropdown: open it, then pick an item by its text.
            await agent.act({"action": "goto", "url": library + "/results.html?again"})
            obs = await agent.observe()
            await agent.act({"action": "click", "ref": ref_in_line(obs, "检索字段", "<button")})
            await agent.act({"action": "click", "text": "篇名"})
            assert await agent.page.inner_text("#field") == "篇名"
            # Last page: next_page says so instead of clicking a disabled button.
            obs = await agent.observe()
            await agent.act({"action": "click", "ref": ref_in_line(obs, "下一页", "<button>3")})
            obs = await agent.observe()
            assert "口罩相关研究第21篇" in obs
            assert "最后一页" in await agent.act({"action": "next_page"})
            # find shows element numbers next to each hit.
            found = await agent.act({"action": "find", "text": "第25篇|不存在的词"})
            assert re.search(r"第25篇.*\[\d+\]<a>详情", found) and "页面中没有“不存在的词”" in found

    asyncio.run(run())


def test_element_numbers_stay_the_same_across_observations(library, chrome):  # noqa: F811
    async def run():
        async with Browser(chrome) as browser:
            agent = Agent(browser, None, [], None, log=lambda *_: None)
            agent.page = await agent._open_tab()
            await agent.act({"action": "goto", "url": library + "/results.html"})
            first = await agent.observe()
            ref = ref_in_line(first, "口罩相关研究第3篇", "<a>详情")
            await agent.act({"action": "scroll", "direction": "down"})
            await agent.act({"action": "read", "from": 0})
            again = await agent.observe()
            assert ref_in_line(again, "口罩相关研究第3篇", "<a>详情") == ref

    asyncio.run(run())


def test_evidence_check():
    seen = "GB/T 38880-2020 儿童口罩技术规范\n本标准主要起草人：高尚荣、李桂梅、李建全。"
    assert evidence_found("本标准主要起草人：高尚荣、李桂梅、李建全", seen)
    assert evidence_found("GB/T 38880-2020 儿童口罩技术规范……主要起草人：高尚荣", seen)
    assert not evidence_found("本标准由王小明、张大伟负责起草", seen)
    assert evidence_found("", seen)


def test_made_up_evidence_is_sent_back_once(std_site, chrome):  # noqa: F811
    QUESTION = "《儿童口罩技术规范》的标准号是？\nA、GB/T 1\nB、GB/T 2\nC、GB/T 38880-2020\nD、GB/T 3"

    class Model:
        def __init__(self):
            self.prompts = []

        def chat_json(self, system, user, max_tokens=700):
            self.prompts.append(user)
            if "空白页" in user:
                return {"action": "goto", "url": std_site + "/results.html"}
            if "证据核对没通过" in user:
                return {"action": "answer", "answer": "C", "confidence": 0.9, "evidence": "GB/T 38880-2020 儿童口罩技术规范"}
            return {"action": "answer", "answer": "A", "confidence": 0.9, "evidence": "标准号为GB/T 1，由王小明起草"}

    async def run(budget):
        async with Browser(chrome) as browser:
            agent = Agent(browser, Model(), [], None, log=lambda *_: None)
            return await agent.run(parse_question(QUESTION, "single"), budget=budget, close=True)

    result = asyncio.run(run(90))
    assert [s.action["action"] for s in result.steps] == ["goto", "answer待核实"]
    assert result.answer.answer == "C" and result.answer.confidence == 0.9
    # Out of time to check again: the answer stands, with its confidence capped.
    result = asyncio.run(run(12))
    assert result.answer.answer == "A" and result.answer.confidence == 0.6
    assert "证据没在看过的页面原文中找到" in result.answer.reason


def test_pointer_shows_where_the_agent_clicks(library, chrome):  # noqa: F811
    async def run():
        async with Browser(chrome) as browser:
            agent = Agent(browser, None, [], None, log=lambda *_: None)
            agent.page = await agent._open_tab()
            await agent.act({"action": "goto", "url": library + "/results.html"})
            obs = await agent.observe()
            ref = ref_in_line(obs, "同意", "<button>同意")
            box = await agent._element(ref).bounding_box()
            await agent.act({"action": "click", "ref": ref})
            where = await agent.page.evaluate(
                "() => document.getElementById('__qp_pointer').firstChild.getBoundingClientRect().toJSON()")
            # The arrow's tip sits on the middle of the button it clicked.
            assert abs(where["x"] + 3 - (box["x"] + box["width"] / 2)) < 2
            assert abs(where["y"] + 2 - (box["y"] + box["height"] / 2)) < 2
            # It isn't part of what the model reads, and the click still went through.
            obs = await agent.observe()
            assert "__qp_pointer" not in obs and "对话框" not in obs
            # Hidden while a screenshot is taken for the model.
            async with agent.pointer.hidden(agent.page):
                shown = await agent.page.evaluate("() => document.getElementById('__qp_pointer').style.display")
            assert shown == "none"
            # It follows to the next page, starting from where it was.
            detail = ref_in_line(obs, "口罩相关研究第2篇", "<a>详情")
            await agent.act({"action": "click", "ref": detail})
            await agent.act({"action": "scroll", "direction": "down"})
            off = Agent(browser, None, [], None, log=lambda *_: None, pointer=False)
            off.page = agent.page
            await off.act({"action": "goto", "url": library + "/results.html?plain"})
            await off.act({"action": "next_page"})
            assert await off.page.evaluate("() => !document.getElementById('__qp_pointer')")

    asyncio.run(run())

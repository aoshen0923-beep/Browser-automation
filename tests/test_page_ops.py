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


@pytest.fixture
def gated(chrome):  # noqa: F811
    """The library site, but PDFs only reach real browser pages (like sites behind Cloudflare)."""
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(SITE_DIR), **kw)

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/members.pdf"):  # needs a login: always answers with the login page
                body = ("<html><body>Login to your account Email/Username Password " + "x" * 3000 + "</body></html>").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.endswith(".pdf") and not self.headers.get("Sec-Fetch-Mode"):
                body = b"<html>Just a moment...</html>"  # the API request is challenged
                self.send_response(403)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_oa_icons_blocked_pdfs_and_figure_captions(gated, chrome):  # noqa: F811
    async def run():
        async with Browser(chrome) as browser:
            agent = Agent(browser, None, [], None, log=lambda *_: None)
            agent.page = await agent._open_tab()
            await agent.act({"action": "goto", "url": gated + "/paper.html"})
            obs = await agent.observe()
            # The text-less lock icon is shown on its own result's line.
            assert re.search(r"Outlier detection.*\[图标:Open Access\]", obs), obs
            assert "Embedding Democratic Values" in obs and not re.search(r"Democratic Values.*图标", obs)
            # The API download is challenged; reading from inside the page works.
            out = await agent.act({"action": "pdf", "url": gated + "/gated.pdf", "page": 2})
            assert "共2页" in out and "图注 2 个（Figure 1、Figure 2）" in out and "表注 1 个（Table 1）" in out, out
            # A login page served at a PDF address is reported as such, never as a "10-page PDF".
            out = await agent.act({"action": "pdf", "url": gated + "/members.pdf"})
            assert "拿到的不是PDF" in out and "Login to your account" in out and "共" not in out.split("（")[0], out
            # A download link hands over the file, which is read at once.
            agent._pdf_cache.clear()
            obs = await agent.observe()
            out = await agent.act({"action": "click", "ref": ref_in_line(obs, "下载全文", "<a")})
            assert "下载了文件" in out and "共2页" in out, out

    asyncio.run(run())


def test_open_many_checks_several_pages_at_once(gated, chrome):  # noqa: F811
    async def run():
        async with Browser(chrome) as browser:
            agent = Agent(browser, None, [], None, log=lambda *_: None)
            agent.page = await agent._open_tab()
            await agent.act({"action": "goto", "url": gated + "/paper.html"})
            tabs = len(browser.context.pages)
            t0 = asyncio.get_running_loop().time()
            out = await agent.act({"action": "open_many", "find": "Open Access|基金|Figure 1", "urls": [
                gated + "/paper.html?x",
                gated + "/detail.html?id=17",  # plain page
                gated + "/gated.pdf",          # a guarded PDF
                gated + "/results.html",       # a page whose list loads by script
            ]})
            took = asyncio.get_running_loop().time() - t0
            assert "【1】" in out and re.search(r"“Open Access”：Outlier detection", out), out
            assert "【2】" in out and "基于深度学习的儿童口罩佩戴检测研究" in out
            assert "【3】" in out and "共2页" in out
            assert "【4】" in out and "口罩相关研究第1篇" in out
            # The other background tabs close; the agent now stands on the first page, ready to click there.
            assert len(browser.context.pages) == tabs + 1
            assert agent.page.url.endswith("/paper.html?x") and "现在停在【1】" in out
            obs = await agent.observe()
            assert "Outlier detection" in obs
            from quizpilot.agent import Step, _repeats

            same = {"action": "open_many", "urls": [gated + "/detail.html?id=17", gated + "/paper.html?x"]}
            other = {"action": "open_many", "urls": [gated + "/c1.html"]}
            steps = [Step({"action": "open_many", "urls": [gated + "/paper.html?x", gated + "/detail.html?id=17"]}, "")]
            assert "已经同时打开过" in _repeats(same, steps)
            assert _repeats(other, steps) == ""
            assert "连续用了两次" in _repeats(other, steps + [Step({"action": "open_many", "urls": ["x"]}, "")])
            return took

    took = asyncio.run(run())
    assert took < 15, took


def test_count_and_folded_filters(library, chrome):  # noqa: F811
    async def run():
        async with Browser(chrome) as browser:
            agent = Agent(browser, None, [], None, log=lambda *_: None)
            agent.page = await agent._open_tab()
            await agent.act({"action": "goto", "url": library + "/results.html"})
            obs = await agent.observe()
            # A folded filter panel says so; its options only appear after the click.
            assert re.search(r"\[\d+\]<button 已折叠-点开才能看到里面的选项>Access Filter", obs), obs
            assert "Open access content only" not in obs
            await agent.act({"action": "click", "ref": ref_in_line(obs, "Access Filter", "<button")})
            obs = await agent.observe()
            assert "已展开>Access Filter" in obs and "Open access content only" in obs
            # Counting is done by the program, not by eye.
            out = await agent.act({"action": "count", "text": "详情"})
            assert "共 10 行" in out and "1. 口罩相关研究第1篇" in out
            out = await agent.act({"action": "count", "text": "详情", "from": "第3篇", "to": "第7篇"})
            assert "共 3 行" in out and "第4篇" in out and "第7篇" not in out.split("\n", 1)[1]
            assert "没有“不存在”" in await agent.act({"action": "count", "text": "x", "from": "不存在"})

    asyncio.run(run())


def test_fixes_from_the_exported_run_records(library, chrome):  # noqa: F811
    async def run():
        async with Browser(chrome) as browser:
            agent = Agent(browser, None, [], None, log=lambda *_: None)
            agent.page = await agent._open_tab()
            await agent.act({"action": "goto", "url": library + "/toc.html"})
            obs = await agent.observe()
            # Science TOC: the section menu mentions RESEARCH ARTICLES before the heading does.
            out = await agent.act({"action": "count", "text": "Download PDF", "from": "Research Articles", "to": "Reports"})
            assert "共 8 行" in out, out
            # CNKI date boxes are read-only pickers: typing still sets them.
            assert "只读" in obs
            ref = ref_in_line(obs, "Date range", "<input")
            assert (await agent.act({"action": "type", "ref": ref, "text": "2022-01-01"})) == "已输入"
            assert await agent.page.input_value("#d1") == "2022-01-01"
            # VIP's orange unlock icon is a text-less <i class="icon-kaisuo">, on its own paper's line.
            assert re.search(r"口服抗凝药物致严重皮肤不良反应的文献分析 \[图标:Open Access\]", obs), obs
            assert not re.search(r"二甲双胍.*图标", obs)

    asyncio.run(run())


def test_no_blind_retries_and_checklist_marks_are_checked():
    from quizpilot.agent import Step, _repeats
    from quizpilot.question import parse_question

    page = "https://www.cell.com/cell"
    steps = [Step({"action": "click", "ref": 57}, "已点击，但页面没有任何变化：…", page)]
    assert "没有任何变化" in _repeats({"action": "click", "ref": 57}, steps, page)
    assert _repeats({"action": "click", "ref": 58}, steps, page) == ""
    assert _repeats({"action": "click", "ref": 57}, steps, "https://www.cell.com/other") == ""
    failed = [Step({"action": "type", "ref": 227, "text": "2022"}, "失败：TimeoutError: Timeout 5000ms exceeded.", page)]
    assert "失败" in _repeats({"action": "type", "ref": 227, "text": "2022"}, failed, page)

    q = parse_question("在《cell》官网的高级检索中，可选的检索字段包括（ ）。\nA. Article Title\nB. Authors\nC. DOI\nD. Affiliation", "multi")
    agent = Agent(None, None, [], None, log=lambda *_: None)
    agent._remember("Search within: All Fields | Article Title | Authors | Keywords")
    agent._checks = {"A": "对：下拉里有", "B": "对：下拉里有", "C": "对：同一字段下拉列表", "D": "错：没看到"}
    notes = dict(agent._suspicious(q))
    assert set(notes) == {"C"} and "DOI" in notes["C"]  # C was marked right without ever seeing "DOI"
    assert agent._unchecked(q) == ["C"]
    tf = parse_question("Article Type 筛选项下的选项包括（）\nA. Article\nB. Review Article\nC. Book Review\nD. Editorial", "multi")
    agent._seen, agent._seen_norm = [], []
    agent._remember("Article Type: Research Article (2893849) / Review Article (95345) / Book Review / Editorial")
    agent._checks = {"A": "错：没有独立的Article", "B": "对", "C": "对", "D": "对"}
    assert "出现过" in dict(agent._suspicious(tf))["A"]  # 'Article' does appear (as Research Article)


def test_slow_catalogs_background_checks_and_no_blank_tab(library, chrome):  # noqa: F811
    from quizpilot.agent import _dead_url_hint, _tracker

    logs = []

    async def run():
        async with Browser(chrome) as browser:
            agent = Agent(browser, None, [], None, log=logs.append)
            assert agent.page is None
            assert await agent.observe() == "（空白页，还没有打开任何网站）"

            async def person():  # solves the verification once the banner asks for it
                while not any("需要人机验证" in line for line in logs):
                    await asyncio.sleep(0.2)
                page = next(p for p in browser.context.pages if p.url.endswith("/verify.html"))
                await page.click("#ok")

            helper = asyncio.create_task(person())
            out = await agent.act({"action": "open_many", "find": "HA34|General Collections", "urls": [
                library + "/spa.html", library + "/verify.html"]})
            await helper
            # The empty-at-first catalog was read once its results arrived.
            assert "HA34.P65 2018" in out, out
            # The verification page got the banner, waited for the person, then was read.
            assert "General Collections" in out.split("【2】")[1], out
            assert any("验证已通过" in line for line in logs)
            # No empty tab was ever put in front: the agent stands on the first page it read.
            assert agent.page.url.endswith("/spa.html")
            assert not any(p.url == "about:blank" for p in agent.opened if not p.is_closed())
            # A page whose document is being replaced by a script doesn't break the reader.
            await agent.page.evaluate("document.removeChild(document.documentElement)")
            snap = await agent._render()
            assert snap["text"] == "" and snap["scroll"]["y"] == 0

    asyncio.run(run())
    assert "search" in _dead_url_hint("失败：Error: net::ERR_NAME_NOT_RESOLVED at https://opac.cqu.edu.cn/")
    assert _dead_url_hint("失败：TimeoutError") == ""
    assert _tracker("https://hm.baidu.com/hm.js?1") and not _tracker("https://opaclib.hainanu.edu.cn/opac/search")

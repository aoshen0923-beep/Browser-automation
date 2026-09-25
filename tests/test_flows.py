import asyncio
import functools
import http.server
import threading

import pytest

from quizpilot.agent import Agent
from quizpilot.browser import Browser
from quizpilot.flows import Recorder, build_flow, replay
from quizpilot.kb import KB
from quizpilot.question import parse_question

from test_browser import chrome  # noqa: F401  (fixture: headless Chromium over CDP)

ADV = """<meta charset="utf-8"><title>高级检索</title>
<form action="result.html">
  <label for="field">检索字段</label>
  <select id="field" name="field"><option>主题</option><option>篇名</option><option>作者</option></select>
  <label for="kw">检索词</label><input id="kw" name="kw" type="text">
  <label><input type="checkbox" id="cssci" name="cssci" value="1">CSSCI</label>
  <button type="submit">检索</button>
</form>"""

RESULT = """<meta charset="utf-8"><title>检索结果</title><div id="info"></div>
<a id="viz" target="_blank">作者分布</a>
<script>
  const p = new URLSearchParams(location.search);
  document.getElementById('info').innerText = '结果：' + p.get('field') + '=' + p.get('kw') + (p.get('cssci') ? ' CSSCI' : '');
  document.getElementById('viz').href = 'authors.html?' + p.toString();
</script>"""

AUTHORS = """<meta charset="utf-8"><title>作者分布</title><div id="a"></div>
<script>
  const p = new URLSearchParams(location.search);
  document.getElementById('a').innerText = '作者分布 ' + p.get('field') + ':' + p.get('kw') + ' 发文最多：唐代兴';
</script>"""


@pytest.fixture
def cnki(tmp_path):
    for name, body in {"adv.html": ADV, "result.html": RESULT, "authors.html": AUTHORS}.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


async def record_demo(browser, base):
    """What a person does once: 篇名 = 信息素养, CSSCI, 检索, then 作者分布."""
    rec = Recorder(browser.context)
    page = await rec.start(base + "/adv.html")
    await page.select_option("#field", label="篇名")
    await page.fill("#kw", "信息素养")
    await page.check("#cssci")
    await page.get_by_role("button", name="检索").click()
    await page.wait_for_url("**/result.html*")
    await page.wait_for_function("document.getElementById('viz').href.includes('authors')")
    await page.click("text=作者分布")
    await asyncio.sleep(0.8)
    return build_flow("知网-高级检索-作者分布", "按字段和检索词检索CSSCI论文，查看作者分布", rec.start_url, rec.stop())


def test_record_then_replay_with_new_parameters(cnki, chrome):  # noqa: F811
    async def run():
        async with Browser(chrome) as browser:
            flow = await record_demo(browser, cnki)
            page = await browser.context.new_page()
            end = await replay(browser.context, page, flow, {"检索词": "大数据", "检索字段": "作者"}, log=lambda *_: None)
            text = await end.evaluate("document.body.innerText")
            return flow, end.url, text

    flow, url, text = asyncio.run(run())
    assert [s["kind"] for s in flow.steps] == ["select", "fill", "check", "click", "click"], flow.steps
    assert [p["name"] for p in flow.params] == ["检索字段", "检索词"]
    assert flow.params[1]["example"] == "信息素养"
    assert "authors.html" in url  # followed the new tab
    assert "作者:大数据" in text


def test_agent_runs_a_saved_flow_in_one_step(cnki, chrome):  # noqa: F811
    q = parse_question("2011—2020年间以“四川师范大学”为第一作者单位发表CSSCI论文最多的作者是？ A、谭光辉 B、汪洪亮 C、唐代兴 D、蔡方鹿", "single")
    seen = []

    class Model:
        def chat_json(self, system, user, max_tokens=700):
            seen.append(user)
            if "发文最多" in user:
                return {"actions": [{"action": "answer", "answer": "C", "confidence": 0.9, "evidence": "发文最多：唐代兴"}]}
            assert "已录制的操作流程" in user and "知网-高级检索-作者分布" in user
            return {"actions": [{"action": "flow", "name": "知网-高级检索-作者分布",
                                 "params": {"检索字段": "作者", "检索词": "四川师范大学"}}]}

    async def run(kb):
        async with Browser(chrome) as browser:
            kb.save_flow(await record_demo(browser, cnki))
            agent = Agent(browser, Model(), [], kb, log=lambda *_: None)
            return await agent.run(q, budget=60, close=True)

    with KB(":memory:") as kb:
        result = asyncio.run(run(kb))
        assert result.answer.answer == "C"
        assert [s.action["action"] for s in result.steps] == ["flow"]
        assert len(seen) == 2  # one call to pick the flow, one to read the result
        assert "作者:四川师范大学" in seen[1]


def test_flows_travel_with_kb_export(tmp_path):
    from quizpilot.flows import Flow

    with KB(tmp_path / "a.sqlite") as a, KB(tmp_path / "b.sqlite") as b:
        a.save_flow(Flow("专利检索", "按申请人检索专利", "https://x", [{"kind": "click", "target": {"text": "检索"}}]))
        a.export(tmp_path / "share.sqlite")
        b.merge_from(tmp_path / "share.sqlite")
        assert b.get_flow("专利检索").description == "按申请人检索专利"
        assert b.find_flows("小米公司的专利申请人检索")[0].name == "专利检索"
        b.delete_flow("专利检索")
        assert b.flows() == [] and b.find_flows("专利") == []


@pytest.fixture
def cnki_like():
    import pathlib

    root = pathlib.Path(__file__).parent / "sites" / "cnki"
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


async def record_cnki(browser, base):
    """The sample question's operation, done like a person on a CNKI-like page."""
    rec = Recorder(browser.context)
    page = await rec.start(base + "/adv.html")
    await page.click("#fieldbtn")                     # custom dropdown, not a <select>
    await page.click("#fieldlist >> text=第一单位")
    await page.fill("#txt_1_value1", "四川师范大学")
    await page.select_option("#y1", "2011")
    await page.select_option("#y2", "2020")
    await page.check("#CSSCI")
    await page.click("input[value=检索]")             # <input type=button>, no inner text
    await page.wait_for_selector("#export:not(.hidden)")  # results arrive later, no navigation
    await page.click("#exp")
    async with browser.context.expect_page() as new:
        await page.click("#allres")                   # opens the analysis in a new window
    viz = await new.value
    await viz.wait_for_selector("#tabs:not(.hidden)")
    await viz.click("#t2")                            # 作者 tab
    await asyncio.sleep(0.8)
    return build_flow("知网-高级检索-作者分布", "按字段、检索词、年份、CSSCI检索，看作者分布", rec.start_url, rec.stop())


def test_realistic_cnki_flow_replays_with_other_choices(cnki_like, chrome):  # noqa: F811
    async def run():
        async with Browser(chrome) as browser:
            flow = await record_cnki(browser, cnki_like)
            page = await browser.context.new_page()
            params = {p["name"]: p["example"] for p in flow.params}
            field_param = next(p["name"] for p in flow.params if p["example"] == "第一单位")
            kw_param = next(p["name"] for p in flow.params if p["example"] == "四川师范大学")
            params.update({field_param: "作者", kw_param: "张三"})
            end = await replay(browser.context, page, flow, params, log=lambda *_: None)
            return flow, await end.evaluate("document.body.innerText")

    flow, text = asyncio.run(run())
    kinds = [s["kind"] for s in flow.steps]
    assert kinds == ["click", "choose", "fill", "select", "select", "check", "click", "click", "choose", "click"], kinds
    examples = {p["example"] for p in flow.params}
    assert {"第一单位", "四川师范大学", "2011", "2020"} <= examples, flow.params
    assert "作者分布（作者=张三，2011-2020，CSSCI）" in text, text

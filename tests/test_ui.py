import asyncio
import json
import threading
import time
import urllib.request

from quizpilot.config import Config
from quizpilot.kb import KB
from quizpilot.ui import App

from test_agent import QUESTION, std_site  # noqa: F401  (fixtures)
from test_browser import chrome  # noqa: F401


class Model:
    """Plays both roles: the quick solver and the browsing agent."""

    def chat_json(self, system, user, max_tokens=700):
        if "网站目录" not in system:  # quick local answer
            return {"answer": "A", "confidence": 0.3, "options": {"A": "unknown"}}
        if "主要起草人" in user:
            return {"action": "answer", "answer": "C", "confidence": 0.95, "reason": "名单里没有许伟民"}
        if "共2页" in user:
            return {"action": "pdf", "page": 2}
        return {"action": "goto", "url": SITE + "/std.pdf"}


SITE = ""


def call(url, data=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def test_answering_page_end_to_end(std_site, chrome, tmp_path):  # noqa: F811
    global SITE
    SITE = std_site
    cfg = Config(root=tmp_path)
    cfg.browser.cdp_url = chrome
    with KB(":memory:") as kb:
        app = App(cfg, kb, Model(), [{"module": "11", "title": "标准", "urls": [std_site], "keywords": "标准"}])
        t = threading.Thread(target=app.serve, kwargs={"port": 0, "open_browser": False}, daemon=True)
        t.start()
        for _ in range(50):
            if app.url:
                break
            time.sleep(0.1)
        base = app.url.rstrip("/")
        page = urllib.request.urlopen(base + "/", timeout=5).read().decode("utf-8")
        assert "quizpilot 答题" in page
        assert call(base + "/api/status")["live_available"] is True

        job = call(base + "/api/ask", {"question": QUESTION, "round": "team", "live": True, "budget": 60})
        assert job["options"] == 4
        for _ in range(200):
            full = call(f"{base}/api/jobs/{job['id']}")
            if full["status"] == "done":
                break
            time.sleep(0.2)
        assert full["status"] == "done", full
        assert full["quick"]["answer"] == "A" and full["quick"]["worth"] is True  # 0.3 > 1/5 in team round
        assert full["final"]["answer"] == "C" and full["final"]["worth"] is True
        assert any("pdf" in line for line in full["log"])
        assert call(base + "/api/jobs")[0]["answer"] == "C"

        bad = urllib.request.Request(base + "/api/ask", data=b'{"question": ""}', headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(bad, timeout=5)
            raise AssertionError("empty question accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 400
        # Website logins: open a site in the dedicated browser, mark it, persist.
        assert any(x["url"] == "https://www.cnki.net" for x in call(base + "/api/logins"))
        assert call(base + "/api/logins/add", {"name": "测试站", "url": std_site + "/index.html"})["group"] == "我添加的网站"
        assert call(base + "/api/logins/open", {"url": std_site + "/index.html"}) == {"ok": True}
        assert any(p.url.endswith("/index.html") for p in app._browser.context.pages)
        call(base + "/api/logins/mark", {"url": std_site + "/index.html", "logged_in": True})
        mine = [x for x in call(base + "/api/logins") if x["url"] == std_site + "/index.html"]
        assert mine and mine[0]["logged_in"] is True
        assert (tmp_path / "logins.json").exists()
        # The research agent is told which sites are logged in.
        from quizpilot.agent import Agent

        agent = Agent(app._browser, Model(), [], None, logged_in=app.logins.logged_in_sites())
        assert "测试站" in agent.system and "已在这个浏览器中登录" in agent.system

        app.loop.call_soon_threadsafe(app.loop.stop)
        t.join(10)


def test_record_and_run_a_flow_from_the_page(chrome, tmp_path):  # noqa: F811
    import asyncio

    from test_flows import ADV, AUTHORS, RESULT
    import functools, http.server  # noqa: E401

    for name, body in {"adv.html": ADV, "result.html": RESULT, "authors.html": AUTHORS}.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    site = f"http://127.0.0.1:{server.server_address[1]}"

    cfg = Config(root=tmp_path)
    cfg.browser.cdp_url = chrome
    with KB(":memory:") as kb:
        app = App(cfg, kb, Model(), [])
        t = threading.Thread(target=app.serve, kwargs={"port": 0, "open_browser": False}, daemon=True)
        t.start()
        while not app.url:
            time.sleep(0.1)
        base = app.url.rstrip("/")

        assert call(base + "/api/flows/record", {"url": site + "/adv.html"}) == {"ok": True}

        async def demo():  # the person demonstrating in the browser window
            page = app.recorder.page
            await page.select_option("#field", label="篇名")
            await page.fill("#kw", "信息素养")
            await page.get_by_role("button", name="检索").click()
            await page.wait_for_url("**/result.html*")

        asyncio.run_coroutine_threadsafe(demo(), app.loop).result(20)
        time.sleep(0.5)
        state = call(base + "/api/flows/recording")
        assert state["active"] and state["steps"][:2] == ["选择「检索字段」= 篇名", "输入「检索词」= 信息素养"], state

        saved = call(base + "/api/flows/stop", {"name": "测试流程", "description": "按检索词检索"})
        assert [p["name"] for p in saved["params"]] == ["检索字段", "检索词"]
        assert call(base + "/api/flows/recording") == {"active": False, "steps": []}
        assert call(base + "/api/flows")[0]["name"] == "测试流程"

        ran = call(base + "/api/flows/run", {"name": "测试流程", "params": {"检索词": "大数据"}})
        assert "result.html" in ran["url"] and "kw=%E5%A4%A7%E6%95%B0%E6%8D%AE" in ran["url"]

        call(base + "/api/flows/delete", {"name": "测试流程"})
        assert call(base + "/api/flows") == []
        app.loop.call_soon_threadsafe(app.loop.stop)
        t.join(10)
    server.shutdown()


def test_knowledge_base_tab_end_to_end(std_site, chrome, tmp_path):  # noqa: F811
    import base64

    import pymupdf

    cfg = Config(root=tmp_path)
    cfg.browser.cdp_url = chrome
    sites = [{"module": "11", "title": "标准", "urls": [std_site + "/index.html", std_site + "/std.pdf"], "keywords": ""},
             {"module": "12", "title": "专利", "urls": [std_site + "/results.html"], "keywords": ""}]
    with KB(tmp_path / "kb.sqlite") as kb:
        app = App(cfg, kb, Model(), sites)
        t = threading.Thread(target=app.serve, kwargs={"port": 0, "open_browser": False}, daemon=True)
        t.start()
        while not app.url:
            time.sleep(0.1)
        base = app.url.rstrip("/")
        info = call(base + "/api/kb")
        assert info["docs"] == 0 and info["module_titles"] == {"11": "标准", "12": "专利"}

        assert call(base + "/api/kb/crawl", {"modules": ["11"]}) == {"ok": True}
        for _ in range(100):
            info = call(base + "/api/kb")
            if not info["task"]["running"] and info["task"]["total"]:
                break
            time.sleep(0.2)
        assert info["task"]["ok"] == 2 and info["task"]["total"] == 2, info["task"]
        assert info["modules"] == {"11": 2}

        # Save whatever the dedicated browser shows.
        page = asyncio.run_coroutine_threadsafe(app._browser.context.new_page(), app.loop).result(10)
        asyncio.run_coroutine_threadsafe(page.goto(std_site + "/results.html"), app.loop).result(10)
        cap = call(base + "/api/kb/capture", {"module": "12", "note": "结果页"})
        assert cap["result"].startswith("captured")

        doc = pymupdf.open()
        doc.new_page().insert_text((72, 72), "uploaded regulation text")
        up = call(base + "/api/kb/upload", {"name": "reg.pdf", "data": base64.b64encode(doc.tobytes()).decode(), "module": "1"})
        assert up == {"added": "reg.pdf"}
        info = call(base + "/api/kb")
        assert info["captures"] == 1 and info["files"] >= 2 and info["modules"]["01"] == 1

        exported = urllib.request.urlopen(base + "/api/kb/export", timeout=10).read()
        assert exported[:15] == b"SQLite format 3"
        with KB(tmp_path / "mate.sqlite") as mate:
            mate.add_document("mate-doc", [(None, "队友的资料")], title="队友")
            mate.export(tmp_path / "mate-export.sqlite")
        merged = call(base + "/api/kb/merge", {"data": base64.b64encode((tmp_path / "mate-export.sqlite").read_bytes()).decode()})
        assert merged == {"added": 1, "updated": 0, "kept": 0}
        app.loop.call_soon_threadsafe(app.loop.stop)
        t.join(10)


def test_take_over_stop_and_continue_buttons(std_site, chrome, tmp_path):  # noqa: F811
    from playwright.sync_api import sync_playwright

    from test_browser import _chromium

    class Wanderer:
        def __init__(self):
            self.prompts = []

        def chat_json(self, system, user, max_tokens=700):
            if "网站目录" not in system:
                return {"answer": "A", "confidence": 0.3}
            self.prompts.append(user)
            if "时间到了" in user:
                return {"action": "answer", "answer": "b", "confidence": 0.4}
            if "空白页" in user:
                return {"action": "goto", "url": std_site + "/results.html"}
            time.sleep(0.2)
            return {"action": "find", "text": "nothing"}

    model = Wanderer()
    cfg = Config(root=tmp_path)
    cfg.browser.cdp_url = chrome
    with KB(":memory:") as kb:
        app = App(cfg, kb, model, [])
        t = threading.Thread(target=app.serve, kwargs={"port": 0, "open_browser": False}, daemon=True)
        t.start()
        while not app.url:
            time.sleep(0.1)
        with sync_playwright() as p:
            viewer = p.chromium.launch(executable_path=_chromium(), args=["--no-sandbox"])
            page = viewer.new_page()
            page.goto(app.url)
            page.fill("#q", QUESTION)
            page.fill("#budget", "300")
            page.click("#go")
            page.click("button[data-act=pause]", timeout=15000)  # 我来操作
            page.wait_for_selector(".paused", timeout=10000)
            job = call(app.url.rstrip("/") + "/api/jobs/1")
            assert job["paused"] and job["controls"]
            steps_while_paused = len(model.prompts)
            time.sleep(1.5)
            assert len(model.prompts) == steps_while_paused  # really paused
            page.click("button[data-act=resume]")  # 继续作答
            page.wait_for_selector("button[data-act=pause]", timeout=10000)
            page.click("button[data-act=stop]")  # 停止，马上作答
            page.wait_for_selector("button[data-act=more]", timeout=15000)
            job = call(app.url.rstrip("/") + "/api/jobs/1")
            assert job["status"] == "done" and job["final"]["answer"] == "B"
            assert any("继续：" in line for line in job["log"])
            page.click("button[data-act=more]")  # 继续查找
            page.click("button[data-act=stop]", timeout=10000)
            for _ in range(100):
                job = call(app.url.rstrip("/") + "/api/jobs/1")
                if job["status"] == "done":
                    break
                time.sleep(0.2)
            assert job["status"] == "done" and any("继续查找" in line for line in job["log"])
            assert "上一轮时间到时给出的答案是 B" in model.prompts[-1]
            viewer.close()
        # Buttons for a finished job are refused politely.
        try:
            call(app.url.rstrip("/") + "/api/jobs/1/pause", {})
            raise AssertionError("pause accepted on a finished job")
        except urllib.error.HTTPError as e:
            assert e.code == 400
        app.loop.call_soon_threadsafe(app.loop.stop)
        t.join(10)


def test_research_starts_without_waiting_for_the_quick_answer_and_old_tabs_close(std_site, chrome, tmp_path):  # noqa: F811
    class Slow:
        def chat_json(self, system, user, max_tokens=700):
            if "网站目录" not in system:
                time.sleep(4)  # a hard question: the quick answer thinks for a long time
                return {"answer": "A", "confidence": 0.3}
            if "空白页" in user:
                return {"action": "goto", "url": std_site + "/results.html"}
            return {"action": "answer", "answer": "C", "confidence": 0.9}

    cfg = Config(root=tmp_path)
    cfg.browser.cdp_url = chrome
    with KB(":memory:") as kb:
        app = App(cfg, kb, Slow(), [])
        t = threading.Thread(target=app.serve, kwargs={"port": 0, "open_browser": False}, daemon=True)
        t.start()
        while not app.url:
            time.sleep(0.1)
        base = app.url.rstrip("/")
        first = call(base + "/api/ask", {"question": QUESTION, "round": "team", "live": True, "budget": 60})
        seen_browsing_first = False
        for _ in range(100):
            full = call(f"{base}/api/jobs/{first['id']}")
            if full["log"] and full["quick"] is None:
                seen_browsing_first = True
            if full["status"] == "done":
                break
            time.sleep(0.1)
        assert seen_browsing_first, "the browser waited for the quick answer"
        assert full["final"]["answer"] == "C" and full["quick"]["answer"] == "A"
        first_tabs = list(app.jobs[first["id"]].agent.opened)
        for _ in range(3):
            job = call(base + "/api/ask", {"question": QUESTION + " ", "round": "team", "live": True, "budget": 60})
            for _ in range(100):
                if call(f"{base}/api/jobs/{job['id']}")["status"] == "done":
                    break
                time.sleep(0.1)
        # Only the last questions keep their tabs (for 继续查找); older ones are closed.
        assert all(p.is_closed() for p in first_tabs)
        assert call(f"{base}/api/jobs/{first['id']}")["can_continue"] is False
        assert call(f"{base}/api/jobs/{job['id']}")["can_continue"] is True
        app.loop.call_soon_threadsafe(app.loop.stop)
        t.join(10)

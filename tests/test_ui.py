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
        app.loop.call_soon_threadsafe(app.loop.stop)
        t.join(10)

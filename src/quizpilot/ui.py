"""Local answering page: paste a question in the browser, watch it get answered.

`quizpilot ui` starts a small web server on 127.0.0.1 (only this computer
can reach it) and opens the page. Each submitted question gets the instant
local answer, then optional live research in the dedicated Chrome, with
every step streamed to the page. Several questions can run at once.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from . import __version__
from .question import Question, parse_question
from .solver import ROUNDS, Answer, break_even, solve, worth_answering

LIVE_BUDGET = {"individual": 25, "team": 75}


def answer_dict(q: Question, ans: Answer, round_name: str, urls: list[str] | None = None) -> dict:
    return {
        "answer": ans.answer,
        "confidence": round(ans.confidence, 2),
        "worth": worth_answering(ans.confidence, round_name) if ans.answer else False,
        "break_even": round(break_even(round_name), 2),
        "seconds": round(ans.seconds, 1),
        "reason": ans.reason,
        "error": ans.error,
        "options": [{"letter": k, "text": v, "note": ans.options.get(k, "")} for k, v in q.options.items()],
        "sources": [h.cite() for h in ans.citations[:3]],
        "urls": (urls or [])[-3:],
    }


@dataclass
class Job:
    id: int
    raw: str
    q: Question
    round: str
    live: bool
    budget: float
    status: str = "running"  # running | done
    phase: str = "本地快速作答中…"
    log: list[str] = field(default_factory=list)
    quick: dict | None = None
    final: dict | None = None
    error: str = ""
    started: float = field(default_factory=time.time)
    finished: float | None = None

    def summary(self) -> dict:
        best = self.final or self.quick or {}
        return {
            "id": self.id,
            "stem": self.q.stem[:80],
            "status": self.status,
            "answer": best.get("answer", ""),
            "worth": best.get("worth", False),
        }

    def full(self) -> dict:
        return {
            **self.summary(),
            "kind": self.q.kind,
            "round": self.round,
            "live": self.live,
            "phase": self.phase,
            "log": self.log,
            "quick": self.quick,
            "final": self.final,
            "error": self.error,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
        }


class App:
    def __init__(self, cfg, kb, model, sites, *, live_available: bool = True):
        self.cfg = cfg
        self.kb = kb
        self.model = model
        self.sites = sites
        self.live_available = live_available
        self.jobs: dict[int, Job] = {}
        self._ids = itertools.count(1)
        self.loop: asyncio.AbstractEventLoop | None = None
        self._browser = None
        self._browser_lock: asyncio.Lock | None = None
        self.url = ""
        from .logins import STATE_NAME, Logins

        self.logins = Logins(cfg.resolve(STATE_NAME))

    # --- jobs ----------------------------------------------------------------------

    def submit(self, raw: str, round_name: str, live: bool, budget: float | None, kind: str | None = None) -> Job:
        q = parse_question(raw, kind)
        job = Job(
            next(self._ids), raw, q, round_name, live and self.live_available,
            budget or LIVE_BUDGET[round_name],
        )
        self.jobs[job.id] = job
        asyncio.run_coroutine_threadsafe(self._run(job), self.loop)
        return job

    async def _run(self, job: Job) -> None:
        try:
            ans = await asyncio.to_thread(solve, job.q, self.kb, self.model, self.cfg.solver.top_k)
            job.quick = answer_dict(job.q, ans, job.round)
            if job.live:
                job.phase = "在 Chrome 中查找…"
                await self._live(job)
        except Exception as e:  # report on the page, never kill the server
            job.error = f"{type(e).__name__}: {e}"
        finally:
            job.status = "done"
            job.phase = "完成"
            job.finished = time.time()

    async def _live(self, job: Job) -> None:
        from .agent import Agent

        try:
            browser = await self._get_browser()
        except Exception as e:
            job.log.append(f"Chrome 未连接：{e}")
            return
        agent = Agent(browser, self.model, self.sites, self.kb, log=job.log.append, logged_in=self.logins.logged_in_sites())
        result = await agent.run(job.q, budget=job.budget)
        job.final = answer_dict(job.q, result.answer, job.round, result.urls)

    async def _warm_up(self) -> None:
        """Open/connect the dedicated Chrome in the background at startup."""
        try:
            await self._get_browser()
            print("Chrome connected - log in to sites there if needed.", flush=True)
        except Exception as e:
            print(f"(Chrome not ready yet: {e})", flush=True)

    async def _get_browser(self):
        from .browser import Browser, ensure_chrome

        async with self._browser_lock:
            b = self._browser
            if b is not None and b._browser is not None and b._browser.is_connected():
                return b
            if b is not None:
                try:
                    await b.__aexit__(None, None, None)
                except Exception:
                    pass
            ok = await asyncio.to_thread(
                ensure_chrome, self.cfg.browser.cdp_url, self.cfg.resolve(self.cfg.browser.profile_dir),
                20.0, self.cfg.browser.executable,
            )
            if not ok:
                raise RuntimeError("无法启动 Chrome，请先运行 quizpilot chrome")
            b = self._browser = await Browser(self.cfg.browser.cdp_url).__aenter__()
            return b

    async def open_for_login(self, url: str) -> None:
        """Open a site in the dedicated browser, in front, for you to log in."""
        browser = await self._get_browser()
        page = await browser.context.new_page()
        await page.bring_to_front()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

    # --- server --------------------------------------------------------------------

    def make_handler(self):
        app = self
        page = resources.files("quizpilot").joinpath("data/ui.html").read_text(encoding="utf-8")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # keep the console quiet
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, data, code: int = 200) -> None:
                self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                if self.path == "/api/status":
                    return self._json({
                        "version": __version__,
                        "model": app.cfg.llm.model,
                        "live_available": app.live_available,
                        "rounds": {k: {"gain": v[0], "loss": v[1], "budget": LIVE_BUDGET[k]} for k, v in ROUNDS.items()},
                    })
                if self.path == "/api/logins":
                    return self._json(app.logins.sites())
                if self.path == "/api/jobs":
                    return self._json([j.summary() for j in sorted(app.jobs.values(), key=lambda j: -j.id)])
                if self.path.startswith("/api/jobs/"):
                    try:
                        job = app.jobs[int(self.path.rsplit("/", 1)[-1])]
                    except (ValueError, KeyError):
                        return self._json({"error": "not found"}, 404)
                    return self._json(job.full())
                return self._json({"error": "not found"}, 404)

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length", "0"))
                return json.loads(self.rfile.read(length) or b"{}")

            def do_POST(self):
                if self.path.startswith("/api/logins/"):
                    return self._logins(self.path.rsplit("/", 1)[-1])
                if self.path != "/api/ask":
                    return self._json({"error": "not found"}, 404)
                try:
                    data = self._body()
                    raw = str(data.get("question", "")).strip()
                    if len(raw) < 4:
                        return self._json({"error": "题目是空的"}, 400)
                    round_name = data.get("round") if data.get("round") in ROUNDS else "individual"
                    budget = float(data["budget"]) if data.get("budget") else None
                    kind = data.get("kind") if data.get("kind") in ("single", "multi", "judge") else None
                    job = app.submit(raw, round_name, bool(data.get("live", True)), budget, kind)
                except Exception as e:
                    return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                return self._json({"id": job.id, "kind": job.q.kind, "options": len(job.q.options)})

            def _logins(self, action: str):
                try:
                    data = self._body()
                    if action == "open":
                        if not app.live_available:
                            return self._json({"error": "浏览器功能已关闭（--no-live）"}, 400)
                        fut = asyncio.run_coroutine_threadsafe(app.open_for_login(str(data["url"])), app.loop)
                        fut.result(timeout=45)
                        return self._json({"ok": True})
                    if action == "mark":
                        app.logins.mark(str(data["url"]), bool(data.get("logged_in", True)))
                        return self._json({"ok": True})
                    if action == "add":
                        return self._json(app.logins.add(str(data.get("name", "")), str(data["url"])))
                    if action == "import":
                        return self._json({"added": app.logins.import_bookmarks(str(data.get("html", "")))})
                except Exception as e:
                    return self._json({"error": f"{type(e).__name__}: {e}"}, 400)
                return self._json({"error": "not found"}, 404)

        return Handler

    def serve(self, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
        self.loop = asyncio.new_event_loop()
        self._browser_lock = asyncio.Lock()
        server = ThreadingHTTPServer((host, port), self.make_handler())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = self.url = f"http://{host}:{server.server_address[1]}/"
        print(f"quizpilot {__version__} answering page: {url}", flush=True)
        print("Keep this window open. Press Ctrl+C here to stop.", flush=True)
        if open_browser:
            webbrowser.open(url)
        if self.live_available:
            asyncio.run_coroutine_threadsafe(self._warm_up(), self.loop)
        try:
            self.loop.run_forever()
        except KeyboardInterrupt:
            print()
        finally:
            server.shutdown()
            if self._browser is not None:
                try:
                    self.loop.run_until_complete(self._browser.__aexit__(None, None, None))
                except Exception:
                    pass
            self.loop.close()

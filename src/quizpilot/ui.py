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
from pathlib import Path

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
        self.recorder = None
        self.kb_task: dict = {"running": False, "done": 0, "ok": 0, "total": 0, "log": []}

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
        agent = Agent(browser, self.model, self.sites, self.kb, log=job.log.append, logged_in=self.logins.logged_in_sites(),
                      vision=self.cfg.llm.vision != "off")
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

    # --- recorded flows ------------------------------------------------------------

    async def start_recording(self, url: str) -> None:
        from .flows import Recorder

        browser = await self._get_browser()
        if self.recorder is not None:
            self.recorder.stop()
        self.recorder = Recorder(browser.context)
        await self.recorder.start(url)

    def recording_state(self) -> dict:
        from .flows import _merge_steps, describe_step

        if self.recorder is None:
            return {"active": False, "steps": []}
        steps = _merge_steps(self.recorder.events)
        return {"active": True, "start_url": self.recorder.start_url, "steps": [describe_step(s) for s in steps]}

    def stop_recording(self, name: str, description: str, save: bool = True) -> dict | None:
        from .flows import build_flow

        rec, self.recorder = self.recorder, None
        if rec is None:
            raise RuntimeError("没有正在进行的录制")
        events = rec.stop()
        if not save:
            return None
        flow = build_flow(name, description, rec.start_url, events)
        if not flow.steps:
            raise RuntimeError("没有录到任何操作")
        self.kb.save_flow(flow)
        return self.flow_dict(flow)

    @staticmethod
    def flow_dict(flow) -> dict:
        from .flows import describe_step

        return {"name": flow.name, "description": flow.description, "start_url": flow.start_url,
                "params": flow.params, "steps": [describe_step(s) for s in flow.steps]}

    async def run_flow(self, name: str, params: dict) -> str:
        from .flows import replay

        flow = self.kb.get_flow(name)
        if flow is None:
            raise RuntimeError(f"没有流程「{name}」")
        browser = await self._get_browser()
        page = await browser.context.new_page()
        await page.bring_to_front()
        end = await replay(browser.context, page, flow, params, log=lambda *_: None)
        return end.url

    # --- knowledge base (prep without the terminal) --------------------------------

    def kb_task_state(self) -> dict:
        return dict(self.kb_task)

    async def crawl_modules(self, modules: set[str]) -> None:
        jobs = [(s["module"], u) for s in self.sites if s["module"] in modules for u in s["urls"]]
        t = self.kb_task = {"running": True, "done": 0, "ok": 0, "total": len(jobs), "log": []}
        try:
            from .browser import crawl

            browser = await self._get_browser()

            def log(line: str) -> None:
                t["done"] += 1
                t["ok"] += "] ok" in line
                t["log"].append(line)

            await crawl(browser, self.kb, jobs, self.cfg.resolve(self.cfg.kb.downloads), concurrency=3, log=log)
        except Exception as e:
            t["log"].append(f"出错：{type(e).__name__}: {e}")
        finally:
            t["running"] = False

    async def capture(self, module: str, note: str) -> str:
        browser = await self._get_browser()
        return await browser.capture_active(self.kb, module=module, note=note)

    def ingest_upload(self, name: str, data: bytes, module: str) -> str:
        from pathlib import Path as _Path

        from .ingest import ingest_file

        folder = self.cfg.resolve(self.cfg.kb.downloads) / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / _Path(name).name
        path.write_bytes(data)
        if ingest_file(self.kb, path, module=module) is None:
            raise ValueError("只支持 PDF、HTML、TXT、MD、CSV 文件")
        return path.name

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
                if self.path == "/api/kb":
                    return self._json({**app.kb.overview(), "task": app.kb_task_state(),
                                       "module_titles": {x["module"]: x["title"] for x in app.sites}})
                if self.path == "/api/kb/export":
                    import tempfile

                    with tempfile.TemporaryDirectory() as d:
                        data = app.kb.export(Path(d) / "kb.sqlite").read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition", 'attachment; filename="quizpilot-kb.sqlite"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return None
                if self.path == "/api/flows":
                    return self._json([app.flow_dict(f) for f in app.kb.flows()])
                if self.path == "/api/flows/recording":
                    return self._json(app.recording_state())
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
                if self.path.startswith("/api/flows/"):
                    return self._flows(self.path.rsplit("/", 1)[-1])
                if self.path.startswith("/api/kb/"):
                    return self._kb(self.path.rsplit("/", 1)[-1])
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

            def _kb(self, action: str):
                import base64
                import tempfile

                try:
                    data = self._body()
                    if action == "crawl":
                        if not app.live_available:
                            return self._json({"error": "浏览器功能已关闭（--no-live）"}, 400)
                        if app.kb_task.get("running"):
                            return self._json({"error": "已经在抓取中"}, 400)
                        modules = {str(m).zfill(2) for m in data.get("modules", [])}
                        if not modules:
                            return self._json({"error": "请先选择模块"}, 400)
                        asyncio.run_coroutine_threadsafe(app.crawl_modules(modules), app.loop)
                        return self._json({"ok": True})
                    if action == "capture":
                        fut = asyncio.run_coroutine_threadsafe(
                            app.capture(str(data.get("module", "")).zfill(2) if data.get("module") else "", str(data.get("note", ""))),
                            app.loop)
                        return self._json({"result": fut.result(timeout=30)})
                    if action == "upload":
                        name = app.ingest_upload(str(data["name"]), base64.b64decode(data["data"]),
                                                 str(data.get("module", "")).zfill(2) if data.get("module") else "")
                        return self._json({"added": name})
                    if action == "merge":
                        with tempfile.TemporaryDirectory() as d:
                            path = Path(d) / "mate.sqlite"
                            path.write_bytes(base64.b64decode(data["data"]))
                            counts = app.kb.merge_from(path)
                        return self._json(counts)
                except Exception as e:
                    return self._json({"error": f"{type(e).__name__}: {e}"}, 400)
                return self._json({"error": "not found"}, 404)

            def _flows(self, action: str):
                try:
                    data = self._body()
                    if not app.live_available and action in ("record", "run"):
                        return self._json({"error": "浏览器功能已关闭（--no-live）"}, 400)
                    if action == "record":
                        url = str(data.get("url", "")).strip()
                        if not url.startswith(("http://", "https://")):
                            url = "https://" + url
                        asyncio.run_coroutine_threadsafe(app.start_recording(url), app.loop).result(timeout=45)
                        return self._json({"ok": True})
                    if action == "stop":
                        return self._json(app.stop_recording(str(data.get("name", "")), str(data.get("description", ""))))
                    if action == "cancel":
                        app.stop_recording("", "", save=False)
                        return self._json({"ok": True})
                    if action == "delete":
                        app.kb.delete_flow(str(data["name"]))
                        return self._json({"ok": True})
                    if action == "run":
                        params = data.get("params") if isinstance(data.get("params"), dict) else {}
                        fut = asyncio.run_coroutine_threadsafe(app.run_flow(str(data["name"]), params), app.loop)
                        return self._json({"url": fut.result(timeout=120)})
                except Exception as e:
                    return self._json({"error": f"{type(e).__name__}: {e}"}, 400)
                return self._json({"error": "not found"}, 404)

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

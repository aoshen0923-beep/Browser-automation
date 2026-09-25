import asyncio
import functools
import glob
import http.server
import os
import socket
import subprocess
import threading
import time
import urllib.request

import pymupdf
import pytest

from quizpilot.browser import Browser, crawl, looks_like_challenge
from quizpilot.kb import KB


def test_challenge_heuristic():
    assert looks_like_challenge("请完成安全验证", [])
    assert looks_like_challenge("", ["https://static.geetest.com/x"])
    # A query form with a captcha field next to real content is not a block.
    assert not looks_like_challenge("学历查询 " + "内容" * 400 + " 验证码", [])


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _chromium():
    for pattern in ("/opt/pw-browsers/chromium-*/chrome-linux*/chrome",):
        found = sorted(glob.glob(pattern))
        if found:
            return found[-1]
    return None


@pytest.fixture
def site(tmp_path):
    (tmp_path / "index.html").write_text(
        "<html><head><meta charset=\"utf-8\"><title>检索首页</title></head><body><h1>高级检索</h1>"
        "<label>文献类型<select><option>学术期刊</option><option>学位论文</option></select></label>"
        "<button>检索</button></body></html>",
        encoding="utf-8",
    )
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "standard full text")
    doc.save(str(tmp_path / "std.pdf"))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def chrome(tmp_path):
    exe = _chromium()
    if not exe:
        pytest.skip("no chromium available")
    port = _free_port()
    proc = subprocess.Popen(
        [exe, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={tmp_path / 'profile'}",
         "--no-sandbox", "--no-first-run", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/json/version", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    yield url
    proc.terminate()
    proc.wait(10)


def test_crawl_and_capture(site, chrome, tmp_path):
    async def run():
        with KB(":memory:") as kb:
            async with Browser(chrome) as b:
                results = await crawl(b, kb, [("31", site + "/index.html"), ("11", site + "/std.pdf")], tmp_path / "dl")
                assert all(r.startswith("ok") for r in results), results
                page = await b.context.new_page()
                await page.goto(site + "/index.html")
                captured = await b.capture_active(kb, module="31", note="高级检索界面")
                assert captured.startswith("captured")
            hits = kb.search("文献类型 学位论文")
            assert hits and hits[0].module == "31"
            assert "学位论文" in hits[0].text
            assert kb.search("standard full text")[0].page == 1
            assert (tmp_path / "dl" / "std.pdf").exists()
            assert kb.stats()["docs"] == 3

    asyncio.run(run())

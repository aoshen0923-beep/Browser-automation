"""Drive your own Chrome/Edge over CDP: logins persist, CAPTCHAs go to you.

quizpilot never launches a fresh automation browser for real work. It
attaches to the Chrome started by `quizpilot chrome`, which uses a
dedicated profile you log in to once (CNKI, Doubao, CNIPA, ...).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, async_playwright

from .kb import KB
from . import pdftools

# Resource types that slow page loads but never carry answer text.
BLOCKED_RESOURCES = {"image", "media", "font"}

_CHALLENGE_FRAMES = (
    "geetest", "captcha.qq.com", "turing.captcha", "recaptcha", "hcaptcha",
    "challenges.cloudflare", "nocaptcha", "aliyuncs.com/captcha",
)
_CHALLENGE_WORDS = (
    "验证码", "安全验证", "人机验证", "滑动验证", "拖动滑块", "向右滑动", "请完成验证",
    "访问验证", "异常访问", "captcha", "verify you are human", "are you a robot",
)


def looks_like_challenge(text: str, frame_urls: list[str]) -> bool:
    """Heuristic for a page that is blocked behind a CAPTCHA/verification.

    Many query forms (chsi, zxgk) always show a captcha input next to real
    content, so words alone only count when the page has little else on it.
    """
    lowered = text.lower()
    if any(marker in url.lower() for url in frame_urls for marker in _CHALLENGE_FRAMES):
        return True
    return len(text.strip()) < 600 and any(w in lowered for w in _CHALLENGE_WORDS)


def alert(message: str) -> None:
    print(f"\a\n>>> {message}", flush=True)
    if sys.platform == "win32":
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass


async def page_blocked(page: Page) -> bool:
    try:
        text = await page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 4000)")
    except Exception:
        return False
    return looks_like_challenge(text, [f.url for f in page.frames])


async def wait_for_human(page: Page, timeout: float = 180) -> bool:
    """Bring a blocked page to the front and wait until the challenge clears."""
    await page.bring_to_front()
    alert(f"Verification needed on {page.url} - solve it in the browser window.")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(1)
        if not await page_blocked(page):
            print(">>> Cleared, continuing.", flush=True)
            return True
    print(">>> Timed out waiting for verification.", flush=True)
    return False


@dataclass
class Snapshot:
    url: str
    title: str
    text: str


async def snapshot(page: Page, aria_limit: int = 20000) -> Snapshot:
    """Visible text plus the accessibility tree (buttons, menus, form options)."""
    title = await page.title()
    text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    try:
        aria = await page.locator("body").aria_snapshot(timeout=5000)
    except Exception:
        aria = ""
    if aria:
        text = f"{text}\n\n[页面结构]\n{aria[:aria_limit]}"
    return Snapshot(page.url, title, text)


async def block_heavy_resources(page: Page) -> None:
    async def handler(route):
        if route.request.resource_type in BLOCKED_RESOURCES:
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", handler)


class Browser:
    """Async context manager attaching to the user's Chrome over CDP."""

    def __init__(self, cdp_url: str):
        self.cdp_url = cdp_url
        self._pw = None
        self._browser = None
        self.context: BrowserContext | None = None

    async def __aenter__(self) -> "Browser":
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
        except Exception as e:
            await self._pw.stop()
            raise RuntimeError(
                f"Can't reach Chrome at {self.cdp_url}. Start it with `quizpilot chrome` first."
            ) from e
        contexts = self._browser.contexts
        self.context = contexts[0] if contexts else await self._browser.new_context()
        return self

    async def __aexit__(self, *exc: object) -> None:
        # Disconnect only; the user's Chrome keeps running.
        if self._pw:
            await self._pw.stop()

    async def active_page(self) -> Page:
        pages = self.context.pages
        if not pages:
            raise RuntimeError("No open tabs in Chrome.")
        for page in reversed(pages):
            try:
                if await page.evaluate("() => document.visibilityState === 'visible' && document.hasFocus()"):
                    return page
            except Exception:
                continue
        for page in reversed(pages):
            try:
                if await page.evaluate("() => document.visibilityState === 'visible'"):
                    return page
            except Exception:
                continue
        return pages[-1]

    async def fetch_into_kb(
        self,
        url: str,
        kb: KB,
        *,
        module: str = "",
        downloads: Path | None = None,
        timeout_ms: int = 30000,
        close: bool = True,
    ) -> str:
        """Open a URL in a new tab, store its content in the KB, return a status line."""
        page = await self.context.new_page()
        try:
            await block_heavy_resources(page)
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            ctype = (response.headers.get("content-type", "") if response else "").lower()
            if "pdf" in ctype or urlparse(url).path.lower().endswith(".pdf"):
                return await self._store_pdf(url, kb, module, downloads)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass  # busy pages never go idle; the DOM is loaded, carry on
            if await page_blocked(page):
                # The CAPTCHA picture itself is an image: stop blocking and reload.
                await page.unroute("**/*")
                await page.reload(wait_until="domcontentloaded")
                await wait_for_human(page)
            snap = await snapshot(page)
            kb.add_document(url, [(None, snap.text)], title=snap.title, kind="page", module=module)
            return f"ok    {url}  ({len(snap.text)} chars)"
        except Exception as e:
            return f"fail  {url}  {type(e).__name__}: {str(e).splitlines()[0][:120]}"
        finally:
            if close:
                await page.close()

    async def _store_pdf(self, url: str, kb: KB, module: str, downloads: Path | None) -> str:
        # APIRequestContext shares the browser's cookies, so logged-in PDFs work.
        resp = await self.context.request.get(url, timeout=60000)
        data = await resp.body()
        if downloads:
            downloads.mkdir(parents=True, exist_ok=True)
            name = Path(urlparse(url).path).name or "download.pdf"
            (downloads / (name if name.endswith(".pdf") else name + ".pdf")).write_bytes(data)
        with pdftools.open_pdf(data) as doc:
            title = (doc.metadata or {}).get("title") or Path(urlparse(url).path).name
            kb.add_document(url, pdftools.page_texts(doc), title=title, kind="pdf", module=module)
            return f"ok    {url}  (PDF, {doc.page_count} pages)"

    async def capture_active(self, kb: KB, *, module: str = "", note: str = "") -> str:
        """Store whatever the current tab shows (e.g. an opened menu) in the KB."""
        page = await self.active_page()
        snap = await snapshot(page)
        title = f"{snap.title} - {note}" if note else snap.title
        source = f"{snap.url}#capture-{int(time.time())}"
        kb.add_document(source, [(None, snap.text)], title=title, kind="capture", module=module)
        return f"captured {snap.url} ({len(snap.text)} chars)"


async def crawl(browser: Browser, kb: KB, jobs: list[tuple[str, str]], downloads: Path, concurrency: int = 4):
    """Fetch (module, url) pairs into the KB, a few tabs at a time."""
    sem = asyncio.Semaphore(concurrency)

    async def one(module: str, url: str) -> str:
        async with sem:
            line = await browser.fetch_into_kb(url, kb, module=module, downloads=downloads)
            print(f"[{module}] {line}", flush=True)
            return line

    return await asyncio.gather(*(one(m, u) for m, u in jobs))


# --- launching Chrome ---------------------------------------------------------

def find_browser() -> str | None:
    candidates: list[str] = []
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
            if base:
                candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
            if base:
                candidates.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    return next((c for c in candidates if c and os.path.exists(c)), None)


def launch_browser(cdp_url: str, profile_dir: Path, executable: str | None = None) -> subprocess.Popen:
    exe = executable or find_browser()
    if not exe:
        raise RuntimeError("Chrome/Edge not found; pass --exe with the path to chrome.exe.")
    port = urlparse(cdp_url).port or 9222
    profile_dir.mkdir(parents=True, exist_ok=True)
    # Chrome refuses remote debugging on the default profile, so a dedicated
    # --user-data-dir is required. Log in to your sites once in this window.
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir.resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    return subprocess.Popen(args, creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

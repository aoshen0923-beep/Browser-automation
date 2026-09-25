"""Which sites you have logged in to in the dedicated browser.

The logins themselves (cookies) live in the browser profile and persist on
their own. This file only remembers the list of sites and which ones you
marked as logged in, so the answering page can show it and the research
agent can prefer sites where you have an account.
"""

from __future__ import annotations

import json
import threading
import time
from importlib import resources
from pathlib import Path
from urllib.parse import urlparse

from .bookmarks import parse_bookmarks

STATE_NAME = "logins.json"


def _defaults() -> list[dict]:
    text = resources.files("quizpilot").joinpath("data/login_sites.json").read_text(encoding="utf-8")
    return json.loads(text)


def _norm(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


class Logins:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.extra: list[dict] = []  # sites you added or imported
        self.status: dict[str, dict] = {}  # url -> {"logged_in": bool, "at": ts}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.extra = data.get("extra", [])
                self.status = data.get("status", {})
            except (OSError, ValueError):
                pass  # a broken file shouldn't stop the page; it is rewritten on the next change

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"extra": self.extra, "status": self.status}, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def sites(self) -> list[dict]:
        seen: set[str] = set()
        out = []
        for s in _defaults() + self.extra:
            if s["url"] in seen:
                continue
            seen.add(s["url"])
            st = self.status.get(s["url"], {})
            out.append({**s, "logged_in": bool(st.get("logged_in")), "at": st.get("at")})
        return out

    def mark(self, url: str, logged_in: bool) -> None:
        with self._lock:
            self.status[url] = {"logged_in": logged_in, "at": time.time()}
            self._save()

    def add(self, name: str, url: str, group: str = "我添加的网站", login: bool = True) -> dict:
        url = _norm(url)
        if not urlparse(url).netloc:
            raise ValueError("网址不正确")
        site = {"name": name.strip() or urlparse(url).netloc, "url": url, "group": group or "我添加的网站", "login": login}
        with self._lock:
            if all(s["url"] != url for s in self.sites()):
                self.extra.append(site)
                self._save()
        return site

    def import_bookmarks(self, html: str) -> int:
        known = {s["url"] for s in self.sites()}
        new = [dict(b, login=True) for b in parse_bookmarks(html) if b["url"] not in known]
        with self._lock:
            self.extra.extend(new)
            self._save()
        return len(new)

    def logged_in_sites(self) -> list[dict]:
        return [s for s in self.sites() if s["logged_in"]]

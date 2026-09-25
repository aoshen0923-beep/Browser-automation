"""Read browser bookmark exports (the Netscape HTML format Chrome/Edge write)."""

from __future__ import annotations

from html.parser import HTMLParser


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.groups: list[str] = []
        self.pending_group: str | None = None
        self.in_h3 = False
        self.in_a = False
        self.href = ""
        self.text = ""
        self.items: list[dict] = []

    def handle_starttag(self, tag, attrs):
        if tag == "h3":
            self.in_h3, self.text = True, ""
        elif tag == "a":
            self.in_a, self.text = True, ""
            self.href = dict(attrs).get("href", "")
        elif tag == "dl" and self.pending_group is not None:
            self.groups.append(self.pending_group)
            self.pending_group = None

    def handle_endtag(self, tag):
        if tag == "h3":
            self.in_h3 = False
            self.pending_group = self.text.strip()
        elif tag == "a":
            self.in_a = False
            if self.href.startswith(("http://", "https://")):
                # The innermost folder names the group; the outer one is usually
                # just the collection's name.
                group = self.groups[-1] if self.groups else ""
                self.items.append({"name": self.text.strip() or self.href, "url": self.href, "group": group})
        elif tag == "dl" and self.groups:
            self.groups.pop()

    def handle_data(self, data):
        if self.in_h3 or self.in_a:
            self.text += data


def parse_bookmarks(html: str) -> list[dict]:
    """[{name, url, group}] in file order, without duplicate URLs."""
    p = _Parser()
    p.feed(html)
    seen: set[str] = set()
    out = []
    for item in p.items:
        if item["url"] not in seen:
            seen.add(item["url"])
            out.append(item)
    return out

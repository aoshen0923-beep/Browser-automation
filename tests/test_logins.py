from quizpilot.bookmarks import parse_bookmarks
from quizpilot.logins import Logins

BOOKMARKS = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<DL><p>
<DT><H3>AI+信息素养大赛</H3>
<DL><p>
<DT><H3>4 学术信息资源</H3>
<DL><p>
<DT><A HREF="https://www.cnki.net">21 CNKI</A>
<DT><A HREF="https://example.org/new">99 新网站</A>
<DT><A HREF="https://www.cnki.net">21 CNKI again</A>
</DL><p>
<DT><A HREF="javascript:void(0)">not a site</A>
</DL><p>
</DL><p>"""


def test_parse_bookmarks_groups_and_dedup():
    items = parse_bookmarks(BOOKMARKS)
    assert items == [
        {"name": "21 CNKI", "url": "https://www.cnki.net", "group": "4 学术信息资源"},
        {"name": "99 新网站", "url": "https://example.org/new", "group": "4 学术信息资源"},
    ]


def test_logins_persist_and_merge(tmp_path):
    path = tmp_path / "logins.json"
    lg = Logins(path)
    sites = lg.sites()
    assert any(s["url"] == "https://www.cnki.net" and s["login"] for s in sites)  # from the bundled list
    assert not any(s["logged_in"] for s in sites)

    lg.mark("https://www.cnki.net", True)
    lg.add("我的学校图书馆", "lib.example.edu.cn")
    assert lg.import_bookmarks(BOOKMARKS) == 1  # only the new one; CNKI already listed

    again = Logins(path)  # reload from disk
    by_url = {s["url"]: s for s in again.sites()}
    assert by_url["https://www.cnki.net"]["logged_in"] is True
    assert by_url["https://lib.example.edu.cn"]["group"] == "我添加的网站"
    assert "https://example.org/new" in by_url
    assert [s["url"] for s in again.logged_in_sites()] == ["https://www.cnki.net"]
    again.mark("https://www.cnki.net", False)
    assert Logins(path).logged_in_sites() == []


def test_broken_state_file_is_ignored(tmp_path):
    path = tmp_path / "logins.json"
    path.write_text("{not json", encoding="utf-8")
    assert Logins(path).sites()

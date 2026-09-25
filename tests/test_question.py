from quizpilot.question import JUDGE, MULTI, SINGLE, parse_question, split_questions


def test_single_with_answer_key():
    q = parse_question(
        "样题（单选题）：\n2018 年我国普通本专科毕业生人数是多少万？（）\n"
        "A、801.2213\nB 、797.1991\nC 、758.5298\nD 、753.3087\n正确答案：D\n答案解析：略"
    )
    assert q.kind == SINGLE
    assert q.options == {"A": "801.2213", "B": "797.1991", "C": "758.5298", "D": "753.3087"}
    assert q.expected == "D"
    assert q.stem.startswith("2018")


def test_inline_options_and_multi_hint():
    q = parse_question("筛选中的“按分类分”，系统提供的筛选项包括以下哪些？（）\nA、科学常识B、时事政治C、公共政策D、公共安全")
    assert q.kind == MULTI
    assert list(q.options.values()) == ["科学常识", "时事政治", "公共政策", "公共安全"]


def test_letters_inside_words_are_not_markers():
    q = parse_question("下列哪些属于该平台提供的功能？\nA、PDF 转Word\nB、PDF 合并\nC、PDF 拆分\nD、PDF 压缩")
    assert q.options["A"] == "PDF 转Word"
    assert q.options["D"] == "PDF 压缩"


def test_dot_markers():
    q = parse_question("哪项功能？（）。\nA. 实验对比\nB. 深度研究\nC. Claim Radar\nD. 私有文档上传", kind=None)
    assert q.options["C"] == "Claim Radar"
    assert q.kind == SINGLE


def test_judge_question():
    q = parse_question("样题（判断题）：\n雨课堂是一个PPT 插件，可以免费安装使用。（）\n正确答案：正确")
    assert q.kind == JUDGE
    assert q.options == {}
    assert q.expected == "对"


def test_explicit_kind_wins():
    q = parse_question("说法对吗？\nA、甲\nB、乙", kind=MULTI)
    assert q.kind == MULTI


def test_split_questions():
    assert split_questions("q1\n---\nq2\n\n----\n") == ["q1", "q2"]


def test_crawl_job_selection(monkeypatch):
    import quizpilot.browser as browser
    from quizpilot import cli

    seen = {}

    async def fake_crawl(b, kb, jobs, downloads, concurrency):
        seen["jobs"] = jobs
        return []

    class FakeBrowser:
        def __init__(self, url):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

    monkeypatch.setattr(browser, "crawl", fake_crawl)
    monkeypatch.setattr(browser, "Browser", FakeBrowser)
    monkeypatch.setattr(cli, "_open_kb", lambda cfg: __import__("quizpilot.kb").kb.KB(":memory:"))
    cli.main(["crawl", "https://x.test/a", "--module", "07"])
    assert seen["jobs"] == [("07", "https://x.test/a")]
    cli.main(["crawl", "--modules", "11"])
    assert seen["jobs"] and all(m == "11" for m, _ in seen["jobs"])

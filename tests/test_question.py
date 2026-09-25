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


def test_interactive_ask_reads_pasted_lines(monkeypatch, capsys):
    from quizpilot import cli
    from quizpilot.solver import Answer

    lines = iter(["《儿童口罩技术规范》以下哪一位不是起草人？（）", "A、高尚荣", "B、李桂梅", "C、许伟民", "D、李建全", ""])

    def fake_input(prompt=""):
        try:
            return next(lines)
        except StopIteration:
            raise EOFError

    seen = []

    def fake_solve(q, kb, model, top_k):
        seen.append(q)
        return Answer("C", 0.9, reason="ok")

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(cli, "_model", lambda cfg: object())
    monkeypatch.setattr(cli, "_open_kb", lambda cfg: __import__("quizpilot.kb").kb.KB(":memory:"))
    monkeypatch.setattr("quizpilot.solver.solve", fake_solve)
    assert cli.main(["ask"]) == 0
    out = capsys.readouterr().out
    assert len(seen) == 1 and seen[0].options["C"] == "许伟民"
    assert "-> single, 4 options" in out and "C    confidence 0.90" in out
    assert "(input closed - exiting)" in out


def test_ask_and_eval_run_the_real_solver(monkeypatch, capsys, tmp_path):
    """End to end through the CLI with the real KB and solver, fake model only."""
    from quizpilot import cli

    class Model:
        def chat_json(self, system, user, max_tokens=700):
            assert "李桂梅" in user  # evidence from the KB reached the prompt
            return {"answer": "C", "confidence": 0.9, "citations": [1]}

    kb_path = tmp_path / "kb.sqlite"
    from quizpilot.kb import KB

    with KB(kb_path) as kb:
        kb.add_document("std", [(2, "本标准主要起草人：高尚荣、李桂梅、李建全。")], title="儿童口罩技术规范")
    monkeypatch.setattr(cli, "_model", lambda cfg: Model())
    monkeypatch.setattr(cli, "_open_kb", lambda cfg: KB(kb_path))
    q = "《儿童口罩技术规范》以下哪一位不是起草人？ A、高尚荣 B、李桂梅 C、许伟民 D、李建全"
    assert cli.main(["ask", q]) == 0
    assert "C    confidence 0.90" in capsys.readouterr().out

    practice = tmp_path / "p.txt"
    practice.write_text("# 模块11（单选题）\n" + q + "\n正确答案：C\n", encoding="utf-8")
    assert cli.main(["eval", str(practice)]) == 0
    out = capsys.readouterr().out
    assert "1/1 correct" in out and "[11]" in out

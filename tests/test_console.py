from quizpilot import cli, console

Q = "《儿童口罩技术规范》以下哪一位不是起草人？（）\r\nA、高尚荣\r\nB、李桂梅\r\nC、许伟民\r\nD、李建全"


def test_looks_like_question():
    assert console.looks_like_question(Q)
    assert console.looks_like_question("雨课堂是一个PPT插件，可以免费安装使用。（）")
    assert not console.looks_like_question("https://openstd.samr.gov.cn")
    assert not console.looks_like_question("ok")


def test_watch_yields_only_new_questions(monkeypatch):
    clips = iter(["old stuff copied before start", "old stuff copied before start", "a url https://x.cn/abc",
                  Q.replace("\r\n", "\n"), Q.replace("\r\n", "\n"), "雨课堂是插件。（）"])
    monkeypatch.setattr(console, "read_clipboard", lambda: next(clips))
    monkeypatch.setattr(console, "clipboard_sequence", lambda: None)
    monkeypatch.setattr(console, "disable_quick_edit", lambda: None)
    got = []
    gen = cli._watch_clipboard(interval=0)
    got.append(next(gen))
    got.append(next(gen))
    assert got[0].startswith("《儿童口罩") and got[1] == "雨课堂是插件。（）"


def test_use_watch_only_in_windows_console(monkeypatch):
    class Args:
        paste = False

    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(console, "has_console_input", lambda: True)
    assert cli._use_watch(Args())
    Args.paste = True
    assert not cli._use_watch(Args())


def test_windows_clipboard_roundtrip():
    import subprocess
    import sys

    import pytest

    if sys.platform != "win32":
        pytest.skip("Windows clipboard API")
    text = "测试题目 A、甲 B、乙"
    r = subprocess.run(["powershell", "-NoProfile", "-Command", f"Set-Clipboard -Value '{text}'"], capture_output=True)
    if r.returncode != 0:
        pytest.skip(f"no clipboard in this session: {r.stderr[:200]!r}")
    assert console.read_clipboard() == text
    console.disable_quick_edit()  # must not raise, even without a console


def test_watch_answers_the_same_question_copied_again(monkeypatch, capsys):
    """Copying identical text again must still trigger (Windows sequence number)."""
    q = Q.replace("\r\n", "\n")
    seqs = iter([5, 5, 6, 6, 7, 8])
    texts = iter([q, q, q, "hello there"])
    monkeypatch.setattr(console, "clipboard_sequence", lambda: next(seqs))
    monkeypatch.setattr(console, "read_clipboard", lambda: next(texts))
    monkeypatch.setattr(console, "disable_quick_edit", lambda: None)
    gen = cli._watch_clipboard(interval=0)
    assert next(gen) == q  # copied again at seq 6, same text as at start
    assert next(gen) == q  # and again at seq 7
    import pytest

    with pytest.raises(RuntimeError):  # fake clipboard exhausted inside the generator
        next(gen)  # seq 8: not a question -> ignored with a note, then clips run out
    assert "not a question" in capsys.readouterr().out

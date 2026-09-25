from quizpilot.kb import KB
from quizpilot.question import parse_question
from quizpilot.solver import break_even, solve, worth_answering


class FakeModel:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def chat_json(self, system, user, max_tokens=700):
        self.prompts.append(user)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def kb_with_standard():
    kb = KB(":memory:")
    kb.add_document("std", [(2, "本标准主要起草人：高尚荣、李桂梅、李建全。")], title="儿童口罩技术规范")
    return kb


Q = "《儿童口罩技术规范》以下哪一位不是该标准的起草人？（）\nA、高尚荣\nB、李桂梅\nC、许伟民\nD、李建全"


def test_answer_with_citation():
    model = FakeModel({"answer": "C", "confidence": 0.9, "citations": [1], "options": {"C": "false"}})
    with kb_with_standard() as kb:
        ans = solve(parse_question(Q, "single"), kb, model)
    assert ans.answer == "C" and ans.confidence == 0.9
    assert ans.citations[0].page == 2
    assert "李桂梅" in model.prompts[0]


def test_invalid_answer_gets_zero_confidence():
    model = FakeModel({"answer": "AC", "confidence": 0.9})
    with kb_with_standard() as kb:
        ans = solve(parse_question(Q, "single"), kb, model)
    assert ans.answer == "" and ans.confidence == 0.0


def test_judge_normalization_and_bad_citations():
    model = FakeModel({"answer": "错误", "confidence": "0.7", "citations": [9, "x"]})
    with kb_with_standard() as kb:
        ans = solve(parse_question("雨课堂是插件。（）", "judge"), kb, model)
    assert ans.answer == "错" and ans.citations == []


def test_model_error_is_reported():
    with kb_with_standard() as kb:
        ans = solve(parse_question(Q), kb, FakeModel(RuntimeError("boom")))
    assert ans.error == "boom" and ans.answer == ""


def test_expected_value_rule():
    assert round(break_even("individual"), 3) == 0.333
    assert round(break_even("team"), 3) == 0.2
    assert worth_answering(0.5, "individual")  # 0.5*2 - 0.5 > 0
    assert not worth_answering(0.25, "individual")  # blind guess on 4 options loses
    assert worth_answering(0.25, "team")  # but pays off at +4/-1

import json

import httpx
import pytest

from quizpilot.config import LLMConfig
from quizpilot.llm import LLMError, OpenAICompatible, parse_json_reply


def test_parse_json_reply_variants():
    assert parse_json_reply('{"a": 1}') == {"a": 1}
    assert parse_json_reply('```json\n{"a": 2}\n```') == {"a": 2}
    assert parse_json_reply('答案如下 {"a": 3} 完') == {"a": 3}
    with pytest.raises(LLMError):
        parse_json_reply("no json")


def test_request_shape_and_errors():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        if seen["body"]["messages"][1]["content"] == "fail":
            return httpx.Response(401, text="bad key")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer": "B"}'}}]})

    cfg = LLMConfig(base_url="https://api.example.com/", model="m1", api_key="k")
    client = OpenAICompatible(cfg, transport=httpx.MockTransport(handler))
    assert client.chat_json("sys", "q") == {"answer": "B"}
    assert seen["url"] == "https://api.example.com/chat/completions"
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == "m1"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    with pytest.raises(LLMError, match="HTTP 401"):
        client.chat_json("sys", "fail")


def test_missing_key():
    with pytest.raises(LLMError, match="DEEPSEEK_API_KEY"):
        OpenAICompatible(LLMConfig(api_key=""))


def test_empty_reply_is_retried_with_a_bigger_budget():
    budgets = []

    def handler(request):
        body = json.loads(request.content)
        budgets.append(body["max_tokens"])
        if len(budgets) == 1:  # all tokens spent on reasoning
            return httpx.Response(200, json={"choices": [{"message": {"content": "", "reasoning_content": "..."}, "finish_reason": "length"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer": "C"}'}, "finish_reason": "stop"}]})

    client = OpenAICompatible(LLMConfig(api_key="k"), transport=httpx.MockTransport(handler))
    assert client.chat_json("s", "u", 400) == {"answer": "C"}
    assert budgets == [3000, 8000]  # the second attempt has more room


def test_empty_twice_raises_clear_error():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})

    client = OpenAICompatible(LLMConfig(api_key="k"), transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError, match="empty or cut-off reply .*length"):
        client.chat_json("s", "u")


def test_parse_json_reply_edge_cases_from_real_runs():
    # Two objects in a row (seen live): keep the first.
    assert parse_json_reply('{"action": "goto", "url": "a"}\n\n{"action": "click", "ref": 5}') == {"action": "goto", "url": "a"}
    # Cut off mid-object (seen live): salvage answer and confidence.
    cut = '{"answer":"C","confidence":0.25,"options":{"A":"unknown：证据未涉及","C":"true：常识'
    got = parse_json_reply(cut)
    assert got["answer"] == "C" and got["confidence"] == "0.25" and got["_partial"]


def test_cut_off_reply_is_retried_then_salvaged():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["max_tokens"])
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer":"B","confidence":0.4,"options":{"A":"fa'}, "finish_reason": "length"}]})

    client = OpenAICompatible(LLMConfig(api_key="k"), transport=httpx.MockTransport(handler))
    got = client.chat_json("s", "u")
    assert got["answer"] == "B" and calls == [3000, 8000, 16000]
    # The quick answer next to live research makes a single attempt (it shouldn't hog the API).
    calls.clear()
    assert client.chat_json("s", "u", attempts=1)["answer"] == "B" and calls == [3000]


def test_network_hiccups_are_retried(monkeypatch):
    import quizpilot.llm as llm

    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    tries = []

    def handler(request):
        tries.append(1)
        if len(tries) < 3:
            raise httpx.ConnectError("[Errno 11001] getaddrinfo failed")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer":"A"}'}, "finish_reason": "stop"}]})

    client = OpenAICompatible(LLMConfig(api_key="k"), transport=httpx.MockTransport(handler))
    assert client.chat_json("s", "u") == {"answer": "A"} and len(tries) == 3


def test_images_are_sent_and_rejection_is_recognized():
    from quizpilot.llm import VisionUnsupported

    seen = {}

    def handler(request):
        body = json.loads(request.content)
        seen["content"] = body["messages"][1]["content"]
        if body["model"] == "text-only":
            return httpx.Response(400, text='{"error":{"message":"unknown variant `image_url`"}}')
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer": "3张"}'}, "finish_reason": "stop"}]})

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
    client = OpenAICompatible(LLMConfig(api_key="k", model="vl"), transport=httpx.MockTransport(handler))
    assert client.chat_json("s", "这页有几张图", images=[png]) == {"answer": "3张"}
    assert seen["content"][0] == {"type": "text", "text": "这页有几张图"}
    assert seen["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")

    text_only = OpenAICompatible(LLMConfig(api_key="k", model="text-only"), transport=httpx.MockTransport(handler))
    with pytest.raises(VisionUnsupported):
        text_only.chat_json("s", "u", images=[png])
    assert text_only.chat_json.__name__  # text requests still go through the normal path

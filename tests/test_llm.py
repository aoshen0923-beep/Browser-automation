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

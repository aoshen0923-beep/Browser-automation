"""Minimal client for OpenAI-compatible chat APIs (DeepSeek by default)."""

from __future__ import annotations

import json
import re
from typing import Protocol

import httpx

from .config import LLMConfig


class LLMError(RuntimeError):
    pass


class ChatModel(Protocol):
    def chat_json(self, system: str, user: str, max_tokens: int = 700) -> dict: ...


def parse_json_reply(content: str) -> dict:
    content = content.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.S)
    if fence:
        content = fence.group(1)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        brace = re.search(r"\{.*\}", content, re.S)
        if brace:
            return json.loads(brace.group(0))
        raise LLMError(f"model did not return JSON: {content[:200]}")


class OpenAICompatible:
    def __init__(self, cfg: LLMConfig, transport: httpx.BaseTransport | None = None):
        if not cfg.api_key:
            raise LLMError(
                "No API key. Set the DEEPSEEK_API_KEY environment variable "
                "or api_key under [llm] in quizpilot.toml."
            )
        self.cfg = cfg
        self.client = httpx.Client(
            base_url=cfg.base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            # Reasoning models can take a while on long pages; older configs
            # said 25s, which cut off live research mid-run.
            timeout=max(float(cfg.timeout), 60.0),
            transport=transport,
        )

    def chat_json(self, system: str, user: str, max_tokens: int = 700) -> dict:
        # Reasoning models can spend the whole token budget thinking and
        # return empty content; retry once with a much larger budget.
        budget = max(max_tokens, 1500)
        for attempt in range(2):
            content, finish = self._complete(system, user, budget)
            if content.strip():
                return parse_json_reply(content)
            budget *= 3
        raise LLMError(f"model returned an empty reply (finish_reason={finish})")

    def _complete(self, system: str, user: str, max_tokens: int) -> tuple[str, str]:
        body = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        try:
            r = self.client.post("/chat/completions", json=body)
        except httpx.HTTPError as e:
            raise LLMError(f"request failed: {e}") from e
        if r.status_code != 200:
            raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
        choice = r.json()["choices"][0]
        return choice["message"].get("content") or "", str(choice.get("finish_reason"))

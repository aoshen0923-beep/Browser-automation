"""Minimal client for OpenAI-compatible chat APIs (DeepSeek by default)."""

from __future__ import annotations

import json
import re
from typing import Protocol

import httpx

from .config import LLMConfig


class LLMError(RuntimeError):
    pass


class VisionUnsupported(LLMError):
    """The model/API rejected image input."""


class ChatModel(Protocol):
    def chat_json(self, system: str, user: str, max_tokens: int = 700, images: list[bytes] | None = None) -> dict: ...


_PARTIAL_FIELDS = {
    "answer": re.compile(r'"answer"\s*:\s*"([^"]*)"'),
    "confidence": re.compile(r'"confidence"\s*:\s*([0-9.]+)'),
    "action": re.compile(r'"action"\s*:\s*"([^"]*)"'),
}


def parse_json_reply(content: str) -> dict:
    """Parse the first JSON object in a reply.

    Tolerates code fences, text around the object, several objects in a row
    (keeps the first) and a reply cut off mid-object, in which case the
    answer/confidence fields that did arrive are salvaged ("_partial").
    """
    content = content.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.S)
    if fence:
        content = fence.group(1)
    start = content.find("{")
    if start >= 0:
        try:
            obj, _ = json.JSONDecoder().raw_decode(content[start:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        salvaged = {}
        for key, pat in _PARTIAL_FIELDS.items():
            m = pat.search(content)
            if m:
                salvaged[key] = m.group(1)
        if "answer" in salvaged:
            salvaged["_partial"] = True
            return salvaged
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

    def chat_json(self, system: str, user: str, max_tokens: int = 700, images: list[bytes] | None = None) -> dict:
        # Reasoning models can spend the whole token budget thinking and
        # return empty content; retry once with a much larger budget.
        budget = max(max_tokens, 1500)
        salvaged: dict | None = None
        for attempt in range(3):  # 1500 → 4500 → 13500 tokens for hard multi-choice questions
            content, finish = self._complete(system, user, budget, images)
            if content.strip():
                try:
                    reply = parse_json_reply(content)
                except LLMError:
                    if finish != "length":
                        raise
                    reply = None
                if reply is not None and not reply.get("_partial"):
                    return reply
                salvaged = salvaged or reply
                if finish != "length":
                    break
            budget *= 3  # cut off (usually by reasoning): retry with more room
        if salvaged:
            return salvaged
        raise LLMError(f"model returned an empty or cut-off reply (finish_reason={finish})")

    def _complete(self, system: str, user: str, max_tokens: int, images: list[bytes] | None = None) -> tuple[str, str]:
        content: object = user
        if images:
            content = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {"url": _data_uri(img)}} for img in images
            ]
        body = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
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
            if images and r.status_code in (400, 415, 422):
                raise VisionUnsupported(f"model rejected image input: HTTP {r.status_code}: {r.text[:200]}")
            raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
        choice = r.json()["choices"][0]
        return choice["message"].get("content") or "", str(choice.get("finish_reason"))


def _data_uri(img: bytes) -> str:
    import base64

    mime = "image/png" if img[:4] == b"\x89PNG" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(img).decode()}"

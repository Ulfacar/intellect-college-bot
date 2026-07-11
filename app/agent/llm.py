"""LLM client adapter.

The agent loop was originally written against Anthropic Messages API objects.
OpenRouter uses an OpenAI-compatible chat API, so this module keeps the old
`client().messages.create(...)` surface and translates requests/responses.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from app.agent.tools import TOOLS
from app.config import settings


@dataclass
class LLMBlock:
    type: str
    text: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None
    id: str | None = None

    def model_dump(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type}
        if self.text is not None:
            data["text"] = self.text
        if self.name is not None:
            data["name"] = self.name
        if self.input is not None:
            data["input"] = self.input
        if self.id is not None:
            data["id"] = self.id
        return data


@dataclass
class LLMResponse:
    stop_reason: str
    content: list[LLMBlock]

    def model_dump(self) -> dict[str, Any]:
        return {"stop_reason": self.stop_reason, "content": [b.model_dump() for b in self.content]}


class OpenRouterMessages:
    async def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        tools: list[dict] | None,
        messages: list[dict],
    ) -> LLMResponse:
        if not settings.openrouter_api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not configured")

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, *_to_openai_messages(messages)],
        }
        converted_tools = _to_openai_tools(tools or [])
        if converted_tools:
            payload["tools"] = converted_tools
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {settings.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        if settings.openrouter_site_url:
            headers["HTTP-Referer"] = settings.openrouter_site_url
        if settings.openrouter_app_name:
            headers["X-Title"] = settings.openrouter_app_name

        async with httpx.AsyncClient(timeout=settings.openrouter_timeout_seconds) as http:
            resp = await http.post(f"{settings.openrouter_base_url.rstrip('/')}/chat/completions", json=payload, headers=headers)
            resp.raise_for_status()
        return _from_openai_response(resp.json())


class OpenRouterClient:
    def __init__(self) -> None:
        self.messages = OpenRouterMessages()


_client: OpenRouterClient | None = None


def llm_enabled() -> bool:
    return bool(settings.openrouter_api_key)


def client() -> OpenRouterClient:
    global _client
    if _client is None:
        _client = OpenRouterClient()
    return _client


async def chat(system: str, messages: list[dict], model: str | None = None) -> dict:
    """Run one LLM turn with tools and return an Anthropic-like dump."""
    resp = await client().messages.create(
        model=model or settings.llm_model_main,
        max_tokens=1024,
        system=system,
        tools=TOOLS,
        messages=messages,
    )
    return resp.model_dump()


def _to_openai_tools(tools: list[dict]) -> list[dict]:
    converted = []
    for tool in tools:
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
        )
    return converted


def _to_openai_messages(messages: list[dict]) -> list[dict]:
    converted: list[dict] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue

        if isinstance(content, list) and role == "assistant":
            text = "\n".join(str(block.get("text", "")) for block in content if block.get("type") == "text").strip()
            tool_calls = []
            for block in content:
                if block.get("type") != "tool_use":
                    continue
                tool_calls.append(
                    {
                        "id": block.get("id") or block.get("name") or "tool_call",
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                        },
                    }
                )
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            converted.append(assistant_msg)
            continue

        if isinstance(content, list) and role == "user" and any(block.get("type") == "tool_result" for block in content):
            for block in content:
                if block.get("type") == "tool_result":
                    converted.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": str(block.get("content", "")),
                        }
                    )
            continue

        converted.append({"role": role, "content": json.dumps(content, ensure_ascii=False)})
    return converted


def _from_openai_response(data: dict[str, Any]) -> LLMResponse:
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    blocks: list[LLMBlock] = []

    content = message.get("content")
    if content:
        blocks.append(LLMBlock(type="text", text=str(content)))

    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw_args = function.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except (TypeError, ValueError):
            args = {}
        blocks.append(
            LLMBlock(
                type="tool_use",
                name=function.get("name", ""),
                input=args,
                id=call.get("id", ""),
            )
        )

    if any(block.type == "tool_use" for block in blocks):
        stop_reason = "tool_use"
    else:
        stop_reason = "end_turn"
    return LLMResponse(stop_reason=stop_reason, content=blocks)

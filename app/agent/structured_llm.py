"""Increment 6: NEW structured OpenRouter client — one forced tool call `emit_response`.

Separate from `app/agent/llm.py` (legacy free-form tool-loop client used by the old
funnel/runner path) — that module is NOT modified, per the task brief. This client is
narrower and stricter: exactly one HTTP call, exactly one tool defined
(`emit_response`), `tool_choice` FORCES the model to call it (no free-text turn, no
multi-turn tool loop). The raw model output (arguments JSON string) is parsed by the
caller (`app/core/ai_reply.py`) via `app/core/ai_schema.py::parse_ai_result` — this
module never validates business schema, only transport/JSON-envelope concerns.

Retry policy (§13 of the brief): timeout / connection error / 429 / 5xx -> at most ONE
retry (these are the only "temporary" failures). 401 -> no retry (bad key, retrying
won't help). 402 -> no retry, treated as a budget/auth error by the caller. Any other
4xx -> no retry. A non-JSON body or a response with no tool call -> no retry (this is a
provider/schema problem, not a transient one) — `error="invalid_json"`/`"no_tool_call"`.

Never raises for "expected" failure modes — always returns a `StructuredCallResult`
with `ok=False` and an `error` code the caller can act on (see `app/core/ai_reply.py`
`_OUTCOME_FOR_ERROR`). It DOES let unexpected exceptions propagate (caller wraps the
whole pipeline in a broad except, same convention as `Orchestrator._run_turn`).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings
from app.core.ai_schema import (
    GRADE_BASES,
    INTENTS,
    LANGUAGES,
    LEAD_STATUSES,
    NEXT_ACTION_TYPES,
)

TOOL_NAME = "emit_response"

# Transient (retry-once) vs terminal (no-retry) transport error codes — used by
# app/core/ai_reply.py to decide log outcome, NOT to retry again (this module already
# performed the one allowed retry internally).
TRANSIENT_ERRORS = frozenset({"timeout", "connection", "http_429", "http_5xx"})
TERMINAL_ERRORS = frozenset({
    "no_api_key", "unauthorized", "payment_required", "http_4xx", "invalid_json", "no_tool_call",
})


@dataclass
class UsageInfo:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int | None = None
    cost: float | None = None
    cost_source: str = "estimated"   # provider | estimated


@dataclass
class StructuredCallResult:
    ok: bool
    arguments: dict[str, Any] | None = None
    usage: UsageInfo | None = None
    latency_ms: float = 0.0
    model: str = ""
    finish_reason: str | None = None
    error: str | None = None            # see TRANSIENT_ERRORS / TERMINAL_ERRORS
    retry_count: int = 0
    generation_id: str | None = None


# --------------------------------------------------------------------------------------
# Tool schema — hand-written (not derived from pydantic $ref/$defs, which some
# OpenAI-compatible providers handle inconsistently for nested models). Kept in sync
# with app/core/ai_schema.py by a regression test (tests/test_ai_schema_sync.py) that
# asserts every enum list here equals the corresponding frozenset in ai_schema.py.
# --------------------------------------------------------------------------------------

def build_emit_response_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "Return the full structured result for this turn: the reply to send "
                "the client, the classification of their message, and any "
                "qualification updates. Call this EXACTLY once, with no other text."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "reply": {"type": "string", "description": "Text to send the client — no markdown."},
                    "language": {"type": "string", "enum": sorted(LANGUAGES)},
                    "answer_basis": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "knowledge_entry_ids": {"type": "array", "items": {"type": "integer"}},
                            "facts_used": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "field": {"type": "string"},
                                        "value": {"type": "string"},
                                        "source_entry_id": {"type": "integer"},
                                    },
                                    "required": ["field", "value", "source_entry_id"],
                                },
                            },
                        },
                        "required": ["knowledge_entry_ids", "facts_used"],
                    },
                    "classification": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "intent": {"type": "string", "enum": sorted(INTENTS)},
                            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                            "evidence": {"type": "string"},
                            "lead_temperature": {"type": "string", "enum": ["new", "cold", "warm", "hot"]},
                            "suggested_status": {
                                "type": ["string", "null"], "enum": sorted(LEAD_STATUSES) + [None],
                            },
                            "next_action_type": {
                                "type": ["string", "null"], "enum": sorted(NEXT_ACTION_TYPES) + [None],
                            },
                            "next_action_at": {"type": ["string", "null"], "description": "ISO 8601 or null."},
                            "should_handoff": {"type": "boolean"},
                            "handoff_reason": {"type": ["string", "null"]},
                            "qualification_updates": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "name": {"type": ["string", "null"]},
                                    "grade_base": {"type": ["string", "null"], "enum": sorted(GRADE_BASES) + [None]},
                                    "direction": {"type": ["string", "null"]},
                                },
                            },
                        },
                        "required": [
                            "intent", "confidence", "evidence", "lead_temperature", "suggested_status",
                            "next_action_type", "next_action_at", "should_handoff", "handoff_reason",
                            "qualification_updates",
                        ],
                    },
                    "summary_update": {"type": "string"},
                    "safety": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "uncertain": {"type": "boolean"},
                            "unsupported_claims": {"type": "array", "items": {"type": "string"}},
                            "requires_human_confirmation": {"type": "boolean"},
                        },
                        "required": ["uncertain", "unsupported_claims", "requires_human_confirmation"],
                    },
                },
                "required": ["reply", "language", "answer_basis", "classification", "summary_update", "safety"],
            },
        },
    }


def _headers() -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }
    if settings.openrouter_site_url:
        headers["HTTP-Referer"] = settings.openrouter_site_url
    if settings.openrouter_app_name:
        headers["X-Title"] = settings.openrouter_app_name
    return headers


def _extract_usage(data: dict[str, Any]) -> UsageInfo:
    usage = data.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
    cached = usage.get("prompt_tokens_details", {}).get("cached_tokens") if isinstance(
        usage.get("prompt_tokens_details"), dict
    ) else None
    provider_cost = usage.get("cost")
    if provider_cost is not None:
        return UsageInfo(
            input_tokens=prompt_tokens, output_tokens=completion_tokens, total_tokens=total_tokens,
            cached_tokens=cached, cost=float(provider_cost), cost_source="provider",
        )
    return UsageInfo(
        input_tokens=prompt_tokens, output_tokens=completion_tokens, total_tokens=total_tokens,
        cached_tokens=cached, cost=None, cost_source="estimated",
    )


def _parse_tool_call(data: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Returns `(arguments, finish_reason)` or `(None, finish_reason)` if no valid tool
    call was found (empty/invalid JSON in the function arguments string)."""
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason")
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        if function.get("name") != TOOL_NAME:
            continue
        raw_args = function.get("arguments") or ""
        try:
            return json.loads(raw_args), finish_reason
        except (TypeError, ValueError):
            return None, finish_reason
    return None, finish_reason


async def call_structured(
    *,
    system: str,
    messages: list[dict[str, Any]],
    model: str,
    max_output_tokens: int,
    timeout_seconds: float,
) -> StructuredCallResult:
    if not settings.openrouter_api_key:
        return StructuredCallResult(ok=False, error="no_api_key", model=model)

    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_output_tokens,
        "messages": [{"role": "system", "content": system}, *messages],
        "tools": [build_emit_response_tool()],
        "tool_choice": {"type": "function", "function": {"name": TOOL_NAME}},
        "usage": {"include": True},   # OpenRouter extension: ask for provider-reported cost
    }
    url = f"{settings.openrouter_base_url.rstrip('/')}/chat/completions"

    retry_count = 0
    attempts_allowed = 2  # first try + one retry, ONLY for transient errors
    while True:
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as http:
                resp = await http.post(url, json=payload, headers=_headers())
        except httpx.TimeoutException:
            latency_ms = (time.monotonic() - started) * 1000
            if retry_count + 1 < attempts_allowed:
                retry_count += 1
                continue
            return StructuredCallResult(ok=False, error="timeout", latency_ms=latency_ms, model=model, retry_count=retry_count)
        except httpx.ConnectError:
            latency_ms = (time.monotonic() - started) * 1000
            if retry_count + 1 < attempts_allowed:
                retry_count += 1
                continue
            return StructuredCallResult(ok=False, error="connection", latency_ms=latency_ms, model=model, retry_count=retry_count)

        latency_ms = (time.monotonic() - started) * 1000

        if resp.status_code == 401:
            return StructuredCallResult(ok=False, error="unauthorized", latency_ms=latency_ms, model=model, retry_count=retry_count)
        if resp.status_code == 402:
            return StructuredCallResult(ok=False, error="payment_required", latency_ms=latency_ms, model=model, retry_count=retry_count)
        if resp.status_code == 429 or resp.status_code >= 500:
            if retry_count + 1 < attempts_allowed:
                retry_count += 1
                continue
            code = "http_429" if resp.status_code == 429 else "http_5xx"
            return StructuredCallResult(ok=False, error=code, latency_ms=latency_ms, model=model, retry_count=retry_count)
        if resp.status_code >= 400:
            return StructuredCallResult(ok=False, error="http_4xx", latency_ms=latency_ms, model=model, retry_count=retry_count)

        try:
            data = resp.json()
        except ValueError:
            return StructuredCallResult(ok=False, error="invalid_json", latency_ms=latency_ms, model=model, retry_count=retry_count)

        arguments, finish_reason = _parse_tool_call(data)
        if arguments is None:
            return StructuredCallResult(
                ok=False, error="no_tool_call", latency_ms=latency_ms, model=model,
                finish_reason=finish_reason, retry_count=retry_count,
            )

        usage = _extract_usage(data)
        generation_id = data.get("id")
        return StructuredCallResult(
            ok=True, arguments=arguments, usage=usage, latency_ms=latency_ms, model=model,
            finish_reason=finish_reason, retry_count=retry_count, generation_id=generation_id,
        )

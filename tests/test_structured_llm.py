"""Increment 6: app/agent/structured_llm.py transport/retry policy (brief §20 errors
52-57 + part of schema 14-18). httpx is monkeypatched at the `httpx.AsyncClient` level
— NO real network call is ever made here."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.agent import structured_llm
from app.config import settings


def _run(coro):
    return asyncio.run(coro)


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict | None = None, *, bad_json: bool = False):
        self.status_code = status_code
        self._json_body = json_body or {}
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("bad json")
        return self._json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)  # type: ignore[arg-type]


class _FakeAsyncClient:
    """Drop-in for `httpx.AsyncClient` — `responses` is a SHARED queue consumed by
    successive `.post()` calls, one per attempt. `call_structured` constructs a new
    `httpx.AsyncClient` per attempt (correct real-world behaviour — connections aren't
    reused across retries here), so the queue must be the SAME list object across
    constructions for a "fails once then succeeds" sequence to actually drain in order."""

    def __init__(self, responses):
        self._responses = responses   # shared reference, NOT a copy

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _patch_client(monkeypatch, responses):
    shared = list(responses)

    def _factory(*args, **kwargs):
        return _FakeAsyncClient(shared)
    monkeypatch.setattr(httpx, "AsyncClient", _factory)


def _tool_call_response(arguments: dict, *, usage=None, finish_reason="tool_calls", gen_id="gen-1"):
    return _FakeResponse(200, {
        "id": gen_id,
        "choices": [{
            "finish_reason": finish_reason,
            "message": {"tool_calls": [{
                "id": "call_1",
                "function": {"name": structured_llm.TOOL_NAME, "arguments": json.dumps(arguments)},
            }]},
        }],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    })


@pytest.fixture(autouse=True)
def _with_api_key(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
    yield


def _call(**overrides):
    kwargs = dict(
        system="sys", messages=[{"role": "user", "content": "hi"}], model="anthropic/claude-haiku-4.5",
        max_output_tokens=512, timeout_seconds=5.0,
    )
    kwargs.update(overrides)
    return structured_llm.call_structured(**kwargs)


# No API key -> immediate failure, no network attempted at all.
def test_no_api_key_short_circuits(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    result = _run(_call())
    assert result.ok is False
    assert result.error == "no_api_key"


# Successful call parses the forced tool call + usage.
def test_successful_call_parses_arguments_and_usage(monkeypatch):
    _patch_client(monkeypatch, [_tool_call_response({"reply": "hi"}, usage={
        "prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160,
    })])
    result = _run(_call())
    assert result.ok is True
    assert result.arguments == {"reply": "hi"}
    assert result.usage.input_tokens == 120
    assert result.usage.output_tokens == 40
    assert result.usage.total_tokens == 160
    assert result.usage.cost_source == "estimated"   # no usage.cost in the fake response
    assert result.generation_id == "gen-1"
    assert result.retry_count == 0


def test_provider_reported_cost_is_preferred(monkeypatch):
    _patch_client(monkeypatch, [_tool_call_response({"reply": "hi"}, usage={
        "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "cost": 0.00042,
    })])
    result = _run(_call())
    assert result.usage.cost == pytest.approx(0.00042)
    assert result.usage.cost_source == "provider"


# 401 -> no retry.
def test_unauthorized_no_retry(monkeypatch):
    responses = [_FakeResponse(401)]
    _patch_client(monkeypatch, responses)
    result = _run(_call())
    assert result.ok is False
    assert result.error == "unauthorized"
    assert result.retry_count == 0


# 402 -> no retry, treated as budget/auth error.
def test_payment_required_no_retry(monkeypatch):
    _patch_client(monkeypatch, [_FakeResponse(402)])
    result = _run(_call())
    assert result.ok is False
    assert result.error == "payment_required"


# 429 -> ONE retry, then succeeds.
def test_429_retries_once_then_succeeds(monkeypatch):
    _patch_client(monkeypatch, [_FakeResponse(429), _tool_call_response({"reply": "ok"})])
    result = _run(_call())
    assert result.ok is True
    assert result.retry_count == 1


# 429 twice -> gives up after exactly one retry.
def test_429_gives_up_after_one_retry(monkeypatch):
    _patch_client(monkeypatch, [_FakeResponse(429), _FakeResponse(429)])
    result = _run(_call())
    assert result.ok is False
    assert result.error == "http_429"
    assert result.retry_count == 1


# 5xx -> ONE retry.
def test_5xx_retries_once(monkeypatch):
    _patch_client(monkeypatch, [_FakeResponse(500), _FakeResponse(500)])
    result = _run(_call())
    assert result.ok is False
    assert result.error == "http_5xx"
    assert result.retry_count == 1


# Timeout -> ONE retry, then succeeds.
def test_timeout_retries_once_then_succeeds(monkeypatch):
    _patch_client(monkeypatch, [httpx.TimeoutException("slow"), _tool_call_response({"reply": "ok"})])
    result = _run(_call())
    assert result.ok is True
    assert result.retry_count == 1


def test_timeout_gives_up_after_one_retry(monkeypatch):
    _patch_client(monkeypatch, [httpx.TimeoutException("slow"), httpx.TimeoutException("slow")])
    result = _run(_call())
    assert result.ok is False
    assert result.error == "timeout"
    assert result.retry_count == 1


# Connection error -> ONE retry.
def test_connection_error_retries_once(monkeypatch):
    _patch_client(monkeypatch, [httpx.ConnectError("down"), httpx.ConnectError("down")])
    result = _run(_call())
    assert result.ok is False
    assert result.error == "connection"
    assert result.retry_count == 1


# Other 4xx -> no retry.
def test_other_4xx_no_retry(monkeypatch):
    _patch_client(monkeypatch, [_FakeResponse(400)])
    result = _run(_call())
    assert result.ok is False
    assert result.error == "http_4xx"
    assert result.retry_count == 0


# Invalid JSON body -> no retry, schema-ish error.
def test_invalid_json_body_no_retry(monkeypatch):
    _patch_client(monkeypatch, [_FakeResponse(200, bad_json=True)])
    result = _run(_call())
    assert result.ok is False
    assert result.error == "invalid_json"


# No tool call in the response -> no retry.
def test_no_tool_call_no_retry(monkeypatch):
    _patch_client(monkeypatch, [_FakeResponse(200, {
        "id": "gen-x", "choices": [{"finish_reason": "stop", "message": {"content": "free text, no tool"}}],
        "usage": {},
    })])
    result = _run(_call())
    assert result.ok is False
    assert result.error == "no_tool_call"


# Malformed tool-call arguments JSON -> no_tool_call (not a crash).
def test_malformed_tool_arguments_no_tool_call(monkeypatch):
    _patch_client(monkeypatch, [_FakeResponse(200, {
        "id": "gen-y",
        "choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{
            "id": "call_1", "function": {"name": structured_llm.TOOL_NAME, "arguments": "{not json"},
        }]}}],
        "usage": {},
    })])
    result = _run(_call())
    assert result.ok is False
    assert result.error == "no_tool_call"


def test_never_raises_for_expected_failure_modes(monkeypatch):
    for responses in (
        [httpx.TimeoutException("x"), httpx.TimeoutException("x")],
        [_FakeResponse(500), _FakeResponse(500)],
        [_FakeResponse(401)],
    ):
        _patch_client(monkeypatch, list(responses))
        result = _run(_call())
        assert result.ok is False  # no exception escaped

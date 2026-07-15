"""Increment 6: app/integrations/panel/ai_log_store.py contract tests — parametrized
over MemoryAiLogStore and PostgresAiLogStore on SQLite-in-memory (StaticPool, no real
network Postgres — same convention as tests/test_leadstore.py)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.integrations.crm.db import init_models
from app.integrations.panel.ai_log_store import MemoryAiLogStore, PostgresAiLogStore

BACKENDS = ["memory", "postgres"]


async def _pg_store() -> PostgresAiLogStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    await init_models(engine)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    return PostgresAiLogStore(sessionmaker=sm)


async def _store(kind: str):
    return MemoryAiLogStore() if kind == "memory" else await _pg_store()


def _run(coro):
    return asyncio.run(coro)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.mark.parametrize("backend", BACKENDS)
def test_reserve_creates_placeholder_row(backend):
    async def scenario():
        store = await _store(backend)
        row = await store.reserve(
            request_id="req-1", conversation_id=1, lead_id=1, bot_id="college_1",
            model="anthropic/claude-haiku-4.5", prompt_version="pilot-v1",
        )
        assert row.outcome == "reserved"
        assert row.id is not None
        fetched = await store.get(row.id)
        assert fetched.request_id == "req-1"
        assert fetched.bot_id == "college_1"
    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_finalize_updates_row_in_place(backend):
    async def scenario():
        store = await _store(backend)
        row = await store.reserve(
            request_id="req-2", conversation_id=1, lead_id=1, bot_id="college_1",
            model="m", prompt_version="pilot-v1",
        )
        updated = await store.finalize(
            row.id, outcome="sent", input_tokens=100, output_tokens=50, total_tokens=150,
            cost=0.00075, cost_source="provider", intent="asks_tuition", confidence=0.95,
            suggested_status="info_sent", applied_status="info_sent", validator_violations=[],
            knowledge_entry_ids=[1, 2],
        )
        assert updated.outcome == "sent"
        assert updated.cost == pytest.approx(0.00075)
        assert updated.cost_source == "provider"
        assert updated.knowledge_entry_ids == [1, 2]
        assert updated.intent == "asks_tuition"
    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_finalize_missing_row_returns_none(backend):
    async def scenario():
        store = await _store(backend)
        assert await store.finalize(999999, outcome="sent") is None
    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_sum_cost_since_filters_by_period(backend):
    async def scenario():
        store = await _store(backend)
        now = _now()
        row1 = await store.reserve(request_id="a", conversation_id=1, lead_id=1, bot_id="b1", model="m", prompt_version="v")
        await store.finalize(row1.id, outcome="sent", cost=1.5)
        row2 = await store.reserve(request_id="b", conversation_id=1, lead_id=1, bot_id="b1", model="m", prompt_version="v")
        await store.finalize(row2.id, outcome="sent", cost=2.5)

        total = await store.sum_cost_since(now - timedelta(minutes=1))
        assert total == pytest.approx(4.0)

        future = await store.sum_cost_since(now + timedelta(days=1))
        assert future == pytest.approx(0.0)
    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_sum_cost_since_filters_by_bot_id(backend):
    async def scenario():
        store = await _store(backend)
        now = _now()
        r1 = await store.reserve(request_id="a", conversation_id=1, lead_id=1, bot_id="bot_a", model="m", prompt_version="v")
        await store.finalize(r1.id, outcome="sent", cost=3.0)
        r2 = await store.reserve(request_id="b", conversation_id=1, lead_id=1, bot_id="bot_b", model="m", prompt_version="v")
        await store.finalize(r2.id, outcome="sent", cost=7.0)

        only_a = await store.sum_cost_since(now - timedelta(minutes=1), bot_id="bot_a")
        assert only_a == pytest.approx(3.0)
        combined = await store.sum_cost_since(now - timedelta(minutes=1))
        assert combined == pytest.approx(10.0)
    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_list_for_conversation(backend):
    async def scenario():
        store = await _store(backend)
        await store.reserve(request_id="a", conversation_id=42, lead_id=1, bot_id="b", model="m", prompt_version="v")
        await store.reserve(request_id="b", conversation_id=42, lead_id=1, bot_id="b", model="m", prompt_version="v")
        await store.reserve(request_id="c", conversation_id=99, lead_id=1, bot_id="b", model="m", prompt_version="v")

        rows = await store.list_for_conversation(42)
        assert len(rows) == 2
    _run(scenario())


def test_never_stores_full_system_prompt_or_secrets():
    """Regression guard: the ORM model has no column named prompt/system/secret/token —
    proves the schema itself cannot accidentally accumulate that data."""
    from app.integrations.crm.db import AiAnswerLog
    columns = {c.name for c in AiAnswerLog.__table__.columns}
    forbidden_substrings = ("prompt_text", "system_prompt", "secret", "api_key", "raw_output")
    for col in columns:
        for forbidden in forbidden_substrings:
            assert forbidden not in col, f"unexpected sensitive column: {col}"

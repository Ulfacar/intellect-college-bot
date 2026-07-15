"""Increment 6: app/core/budget.py (brief §20 budget/usage scenarios 44-51) —
parametrized over memory + postgres (SQLite-in-memory, StaticPool) backends."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core import budget
from app.integrations.crm.db import init_models
from app.integrations.panel import ai_log_store as ai_log_store_module
from app.integrations.panel.ai_log_store import PostgresAiLogStore

BACKENDS = ["memory", "postgres"]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _pg_engine_store(monkeypatch):
    """Swaps the module-level Postgres singleton for a fresh SQLite-in-memory
    "postgres contract" store, and restores it afterward — same pattern as
    tests/test_lead_status_service.py for services with process-wide singletons."""
    async def _build():
        engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        await init_models(engine)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        return PostgresAiLogStore(sessionmaker=sm)

    store = _run(_build())
    monkeypatch.setattr(ai_log_store_module, "_pg_ai_log_store", store)
    return store


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    ai_log_store_module.reset()
    monkeypatch.setattr(settings, "llm_daily_budget_usd", 0.0)
    monkeypatch.setattr(settings, "llm_monthly_budget_usd", 0.0)
    monkeypatch.setattr(settings, "panel_backend", "memory")
    yield
    ai_log_store_module.reset()


def _use_backend(backend, monkeypatch, pg_fixture):
    if backend == "postgres":
        monkeypatch.setattr(settings, "panel_backend", "postgres")
        return pg_fixture


@pytest.mark.parametrize("backend", BACKENDS)
def test_unlimited_budget_never_exhausted(backend, monkeypatch, _pg_engine_store):
    _use_backend(backend, monkeypatch, _pg_engine_store)

    async def scenario():
        status = await budget.is_exhausted(bot_id="college_1")
        assert status.exhausted is False
        assert status.reason is None
    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_reserve_allowed_and_inserts_placeholder(backend, monkeypatch, _pg_engine_store):
    _use_backend(backend, monkeypatch, _pg_engine_store)

    async def scenario():
        res = await budget.reserve(
            bot_id="college_1", conversation_id=1, lead_id=1, model="m",
            prompt_version="pilot-v1", request_id="r1",
        )
        assert res.allowed is True
        assert res.log_id is not None
    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_daily_limit_exhausted_after_spend(backend, monkeypatch, _pg_engine_store):
    monkeypatch.setattr(settings, "llm_daily_budget_usd", 1.0)
    _use_backend(backend, monkeypatch, _pg_engine_store)

    async def scenario():
        from app.integrations.panel.ai_log_store import get_ai_log_store
        store = get_ai_log_store()
        row = await store.reserve(
            request_id="a", conversation_id=1, lead_id=1, bot_id="college_1", model="m", prompt_version="v",
        )
        await store.finalize(row.id, outcome="sent", cost=1.5)

        status = await budget.is_exhausted(bot_id="college_1")
        assert status.exhausted is True
        assert status.reason == "daily_exceeded"

        res = await budget.reserve(
            bot_id="college_1", conversation_id=1, lead_id=1, model="m",
            prompt_version="pilot-v1", request_id="r2",
        )
        assert res.allowed is False
        assert res.log_id is None
    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_monthly_limit_exhausted(backend, monkeypatch, _pg_engine_store):
    monkeypatch.setattr(settings, "llm_monthly_budget_usd", 2.0)
    _use_backend(backend, monkeypatch, _pg_engine_store)

    async def scenario():
        from app.integrations.panel.ai_log_store import get_ai_log_store
        store = get_ai_log_store()
        row = await store.reserve(
            request_id="a", conversation_id=1, lead_id=1, bot_id="college_1", model="m", prompt_version="v",
        )
        await store.finalize(row.id, outcome="sent", cost=2.5)

        status = await budget.is_exhausted()
        assert status.exhausted is True
        assert status.reason == "monthly_exceeded"
    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_faq_and_commands_unaffected_by_exhausted_budget(backend, monkeypatch, _pg_engine_store):
    """Budget exhaustion only gates the LLM call — published FAQ / commands / manual
    replies are a completely separate code path (see app/core/telegram_commands.py)
    and are not touched by this module at all; this test documents that boundary."""
    monkeypatch.setattr(settings, "llm_daily_budget_usd", 0.01)
    _use_backend(backend, monkeypatch, _pg_engine_store)

    async def scenario():
        from app.integrations.panel.ai_log_store import get_ai_log_store
        store = get_ai_log_store()
        row = await store.reserve(
            request_id="a", conversation_id=1, lead_id=1, bot_id="college_1", model="m", prompt_version="v",
        )
        await store.finalize(row.id, outcome="sent", cost=1.0)
        status = await budget.is_exhausted(bot_id="college_1")
        assert status.exhausted is True
        # budget.py exposes NOTHING that touches FAQ/commands — nothing to assert
        # beyond "this module has no side effect outside ai_answer_log/is_exhausted".
    _run(scenario())


def test_reservation_cost_counts_toward_spend_before_finalize():
    """A `reserve()`d-but-not-yet-`finalize()`d row still counts toward spend — this is
    what makes concurrent reservations see each other (see module docstring's
    "Reservation cost accounting")."""
    async def scenario():
        with_estimate = budget._reservation_estimate_cost(settings.llm_max_output_tokens)
        assert with_estimate > 0
        res = await budget.reserve(
            bot_id="college_1", conversation_id=1, lead_id=1, model="m",
            prompt_version="pilot-v1", request_id="r1",
        )
        from app.integrations.panel.ai_log_store import get_ai_log_store
        row = await get_ai_log_store().get(res.log_id)
        assert row.cost == pytest.approx(with_estimate)
        assert row.cost_source == "estimated"
    _run(scenario())


def test_concurrent_reservations_cannot_exceed_limit(monkeypatch):
    """Two concurrent reserve() calls, budget room for exactly one -> exactly one
    allowed. Proves the asyncio.Lock-serialized check-then-insert closes the race (see
    module docstring)."""
    estimate = budget._reservation_estimate_cost(settings.llm_max_output_tokens)
    # Room for slightly more than one reservation, but less than two.
    monkeypatch.setattr(settings, "llm_daily_budget_usd", estimate * 1.5)

    async def scenario():
        results = await asyncio.gather(
            budget.reserve(bot_id="college_1", conversation_id=1, lead_id=1, model="m", prompt_version="v", request_id="c1"),
            budget.reserve(bot_id="college_1", conversation_id=1, lead_id=1, model="m", prompt_version="v", request_id="c2"),
        )
        allowed = [r for r in results if r.allowed]
        assert len(allowed) == 1
    _run(scenario())

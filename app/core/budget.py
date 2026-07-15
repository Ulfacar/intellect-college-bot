"""Increment 6 telegram-pilot: daily/monthly LLM spend budget, checked BEFORE every
structured OpenRouter call — see `app/core/ai_reply.py`.

Two entry points:
- `is_exhausted(...)` — cheap, read-only, no reservation. Used by
  `app/core/telegram_commands.py::route_message` as an early gate (§ pipeline
  integration step "b") so an obviously-exhausted budget never even builds the
  knowledge/prompt for a call that would be refused anyway, and so the reason is
  available to show a manager (e.g. a future admin "AI budget" panel) without writing a
  log row for a mere status check.
- `reserve(...)` — the AUTHORITATIVE atomic check-and-record, called from
  `app/core/ai_reply.py` right before the network call. Inserts a placeholder
  `ai_answer_log` row (`outcome="reserved"`) as part of the SAME check, so a second
  concurrent request's sum-of-spend includes the first request's reservation even
  though its real cost is not known yet (recorded later via
  `app/integrations/panel/ai_log_store.py::finalize`). This closes the classic
  check-then-act race between "read current spend" and "commit to spending more".

Atomicity approach (documented, matches the brief's "document approach" requirement):
- **Postgres**: `pg_advisory_xact_lock` on a key derived from the current UTC day
  serializes ALL reserve attempts (any bot) across worker processes for that day —
  held until the reserving transaction commits or rolls back, so the sum-then-insert
  is race-free even under real concurrent workers. Skipped automatically on non-
  Postgres dialects (see `_advisory_lock`), so the SQLite-in-memory "postgres contract"
  tests (`tests/test_budget.py`, same StaticPool convention as
  `tests/test_lead_status_service.py`) exercise the transaction/insert logic without
  requiring a real Postgres server.
- **Both backends, additionally**: a single process-wide `asyncio.Lock` serializes
  `reserve()` end-to-end. This is the SAME "one worker / sticky" assumption already
  documented elsewhere in this codebase (`app/core/orchestrator.py`'s `_key_locks`,
  `app/core/telegram_sessions.py`'s per-key lock) — it is what actually guarantees
  "concurrent workers cannot exceed the limit" for the in-process test suite and for a
  single-process deployment; multi-process Postgres deployments additionally get the
  advisory lock above. This IS the documented memory-backend limitation the brief asks
  for: across multiple OS processes with `panel_backend=memory`, there is no shared
  state at all, so the guarantee only holds within one process either way.

Reservation cost accounting: the real per-call cost is only known AFTER the network
call (recorded via `finalize`), but a reservation must still count towards the spend
sum immediately, otherwise N concurrent requests could all "see" the same pre-spend
total and all be granted even though the budget only covers one. Each reservation is
therefore inserted with a conservative WORST-CASE cost estimate
(`_reservation_estimate_cost`, based on `settings.llm_max_output_tokens` and a generous
assumed input size) — `finalize()` overwrites it with the real, normally-lower, cost
once known. This is deliberately pessimistic (may refuse a call slightly before the
"true" spend reaches the limit) in favour of never exceeding it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
import zlib

from app.config import settings
from app.integrations.panel.ai_log_store import PostgresAiLogStore, get_ai_log_store

_reserve_lock = asyncio.Lock()

# Same $1 / $5 per 1M input/output rate documented in app/core/ai_reply.py's fallback
# cost estimator (verified OpenRouter catalog price for anthropic/claude-haiku-4.5 at
# increment time) — used here only to compute a conservative WORST-CASE reservation
# cost, never the real per-request cost (which app/core/ai_reply.py records from the
# provider's usage payload whenever available).
RESERVATION_RATE_INPUT_PER_1M = 1.0
RESERVATION_RATE_OUTPUT_PER_1M = 5.0
# Generous upper bound on prompt size: system prompt + knowledge + history capped at
# app/core/ai_reply.py::HISTORY_CHAR_CAP (6000 chars, ~1500 tokens) plus safety-rules
# and knowledge-formatting overhead.
ASSUMED_MAX_INPUT_TOKENS = 4000


def _reservation_estimate_cost(max_output_tokens: int) -> float:
    input_cost = (ASSUMED_MAX_INPUT_TOKENS / 1_000_000) * RESERVATION_RATE_INPUT_PER_1M
    output_cost = (max_output_tokens / 1_000_000) * RESERVATION_RATE_OUTPUT_PER_1M
    return input_cost + output_cost


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _start_of_day(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _start_of_month(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@dataclass
class BudgetStatus:
    exhausted: bool
    reason: str | None            # None | daily_exceeded | monthly_exceeded
    daily_spent: float
    daily_limit: float            # 0.0 = unlimited
    monthly_spent: float
    monthly_limit: float          # 0.0 = unlimited


@dataclass
class Reservation:
    allowed: bool
    log_id: int | None
    status: BudgetStatus


async def _sums(*, bot_id: str | None, now: datetime) -> tuple[float, float]:
    store = get_ai_log_store()
    daily = await store.sum_cost_since(_start_of_day(now), bot_id=bot_id)
    monthly = await store.sum_cost_since(_start_of_month(now), bot_id=bot_id)
    return daily, monthly


def _evaluate(daily_spent: float, monthly_spent: float, *, now: datetime, pending: float = 0.0) -> BudgetStatus:
    """`pending` (default 0) is the WORST-CASE cost of the reservation currently being
    considered — `reserve()` passes its own estimate so the check is "would granting
    THIS reservation push spend to/over the limit", not just "have we already crossed
    it". `is_exhausted()` (the cheap read-only pre-gate) passes the default 0 — it has
    no specific call to project, it only reports whether spend so far is already at
    the limit (see module docstring's "reservation cost accounting" section)."""
    daily_limit = settings.llm_daily_budget_usd or 0.0
    monthly_limit = settings.llm_monthly_budget_usd or 0.0
    if daily_limit and (daily_spent + pending) >= daily_limit:
        return BudgetStatus(True, "daily_exceeded", daily_spent, daily_limit, monthly_spent, monthly_limit)
    if monthly_limit and (monthly_spent + pending) >= monthly_limit:
        return BudgetStatus(True, "monthly_exceeded", daily_spent, daily_limit, monthly_spent, monthly_limit)
    return BudgetStatus(False, None, daily_spent, daily_limit, monthly_spent, monthly_limit)


async def is_exhausted(*, bot_id: str | None = None, now: datetime | None = None) -> BudgetStatus:
    """Cheap read-only check — no reservation, no log row written. See module
    docstring for why `reserve()` is still the authoritative gate."""
    now = now or _now()
    daily_spent, monthly_spent = await _sums(bot_id=bot_id, now=now)
    return _evaluate(daily_spent, monthly_spent, now=now)


async def _advisory_lock(session, key: str) -> None:
    """Best-effort Postgres advisory lock — silently a no-op on any other dialect
    (SQLite test contract, or a future non-Postgres backend). Held until the caller's
    transaction commits/rolls back (`pg_advisory_xact_lock`), serializing concurrent
    `reserve()` calls across worker PROCESSES for the same key."""
    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect != "postgresql":
        return
    from sqlalchemy import text
    lock_key = zlib.crc32(key.encode("utf-8"))
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key})


async def reserve(
    *, bot_id: str, conversation_id: int | None, lead_id: int | None, model: str,
    prompt_version: str, request_id: str, now: datetime | None = None,
) -> Reservation:
    """Authoritative atomic check-and-reserve — see module docstring for the
    concurrency approach. Returns `allowed=False` (no row written) if the budget is
    already exhausted; otherwise inserts a `outcome="reserved"` placeholder row and
    returns its id for `app/core/ai_reply.py` to finalize after the call."""
    now = now or _now()
    store = get_ai_log_store()
    estimate = _reservation_estimate_cost(settings.llm_max_output_tokens)

    async with _reserve_lock:
        if isinstance(store, PostgresAiLogStore):
            sm = store.sessionmaker()
            async with sm() as session:
                async with session.begin():
                    await _advisory_lock(session, f"ai_budget:{now.date().isoformat()}")
                    from sqlalchemy import func as sa_func
                    from sqlalchemy import select as sa_select

                    from app.integrations.crm.db import AiAnswerLog
                    daily = (await session.execute(
                        sa_select(sa_func.coalesce(sa_func.sum(AiAnswerLog.cost), 0.0))
                        .where(AiAnswerLog.created_at >= _start_of_day(now))
                    )).scalar()
                    monthly = (await session.execute(
                        sa_select(sa_func.coalesce(sa_func.sum(AiAnswerLog.cost), 0.0))
                        .where(AiAnswerLog.created_at >= _start_of_month(now))
                    )).scalar()
                    status = _evaluate(float(daily or 0.0), float(monthly or 0.0), now=now, pending=estimate)
                    if status.exhausted:
                        return Reservation(allowed=False, log_id=None, status=status)
                    row = AiAnswerLog(
                        request_id=request_id, conversation_id=conversation_id, lead_id=lead_id,
                        bot_id=bot_id, model=model, prompt_version=prompt_version, outcome="reserved",
                        cost=estimate, cost_source="estimated",
                    )
                    session.add(row)
                    await session.flush()
                    log_id = row.id
                return Reservation(allowed=True, log_id=log_id, status=status)

        daily_spent, monthly_spent = await _sums(bot_id=None, now=now)
        status = _evaluate(daily_spent, monthly_spent, now=now, pending=estimate)
        if status.exhausted:
            return Reservation(allowed=False, log_id=None, status=status)
        row = await store.reserve(
            request_id=request_id, conversation_id=conversation_id, lead_id=lead_id,
            bot_id=bot_id, model=model, prompt_version=prompt_version,
            cost=estimate, cost_source="estimated",
        )
        return Reservation(allowed=True, log_id=row.id, status=status)

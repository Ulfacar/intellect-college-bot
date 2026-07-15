"""Increment 6 telegram-pilot: store for the single additive `ai_answer_log` table —
§14 LLM usage/cost AND §15 answer-context in one row per structured call (see
`app/integrations/crm/db.py::AiAnswerLog`).

Two backends behind one contract (same convention as every other pilot store —
`leadstore.py`, `faq_kb.py`, `audit_store.py`): `MemoryAiLogStore` (default) and
`PostgresAiLogStore` (prod), selected by `settings.panel_backend`.

Lifecycle: `reserve(...)` inserts a placeholder row (`outcome="reserved"`, zero
cost/tokens) BEFORE the network call — this is what `app/core/budget.py` uses as the
atomic spend record so a concurrent request sees it in its own sum. `finalize(...)`
updates that SAME row once the call (or a pre-call failure) is known. Nothing here
decides budget policy — see `app/core/budget.py` for the daily/monthly check built on
top of `sum_cost_since`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AiAnswerLogView:
    id: int
    request_id: str = ""
    generation_id: str | None = None
    conversation_id: int | None = None
    lead_id: int | None = None
    bot_id: str = ""
    model: str = ""
    prompt_version: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    cost: float | None = None
    cost_source: str | None = None
    latency_ms: float | None = None
    outcome: str = "reserved"
    retry_count: int = 0
    client_message_id: str | None = None
    bot_message_id: str | None = None
    source: str = "llm"
    knowledge_entry_ids: list[int] = field(default_factory=list)
    language: str | None = None
    intent: str | None = None
    confidence: float | None = None
    evidence: str | None = None
    suggested_status: str | None = None
    applied_status: str | None = None
    lead_temperature: str | None = None
    bot_phase: str | None = None
    dialog_owner: str | None = None
    validator_violations: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


_RESERVE_FIELDS = {
    "request_id", "conversation_id", "lead_id", "bot_id", "model", "prompt_version",
}


class MemoryAiLogStore:
    def __init__(self) -> None:
        self._rows: dict[int, AiAnswerLogView] = {}
        self._seq = 0

    def _reset(self) -> None:
        self.__init__()  # type: ignore[misc]

    async def reserve(self, **fields: Any) -> AiAnswerLogView:
        self._seq += 1
        now = _now()
        row = AiAnswerLogView(id=self._seq, outcome="reserved", created_at=now, updated_at=now, **fields)
        self._rows[row.id] = row
        return row

    async def finalize(self, log_id: int, **fields: Any) -> AiAnswerLogView | None:
        row = self._rows.get(log_id)
        if row is None:
            return None
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = _now()
        return row

    async def get(self, log_id: int) -> AiAnswerLogView | None:
        return self._rows.get(log_id)

    async def list_for_conversation(self, conversation_id: int) -> list[AiAnswerLogView]:
        return [r for r in self._rows.values() if r.conversation_id == conversation_id]

    async def sum_cost_since(self, since: datetime, *, bot_id: str | None = None) -> float:
        total = 0.0
        for row in self._rows.values():
            if row.created_at is None or row.created_at < since:
                continue
            if bot_id is not None and row.bot_id != bot_id:
                continue
            total += row.cost or 0.0
        return total


class PostgresAiLogStore:
    def __init__(self, sessionmaker: async_sessionmaker | None = None) -> None:
        self._sessionmaker = sessionmaker

    def _sm(self) -> async_sessionmaker:
        if self._sessionmaker is None:
            from app.integrations.crm.db import get_sessionmaker
            self._sessionmaker = get_sessionmaker()
        return self._sessionmaker

    def sessionmaker(self) -> async_sessionmaker:
        """Public access — `app/core/budget.py` needs the SAME sessionmaker to run its
        own atomic (advisory-lock-guarded) reserve transaction, mirroring
        `PostgresLeadStore.sessionmaker()`."""
        return self._sm()

    async def reserve(self, **fields: Any) -> AiAnswerLogView:
        from app.integrations.crm.db import AiAnswerLog
        async with self._sm()() as session:
            row = AiAnswerLog(outcome="reserved", **fields)
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _view(row)

    async def finalize(self, log_id: int, **fields: Any) -> AiAnswerLogView | None:
        from app.integrations.crm.db import AiAnswerLog
        async with self._sm()() as session:
            row = await session.get(AiAnswerLog, log_id)
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
            return _view(row)

    async def get(self, log_id: int) -> AiAnswerLogView | None:
        from app.integrations.crm.db import AiAnswerLog
        async with self._sm()() as session:
            row = await session.get(AiAnswerLog, log_id)
            return _view(row) if row is not None else None

    async def list_for_conversation(self, conversation_id: int) -> list[AiAnswerLogView]:
        from app.integrations.crm.db import AiAnswerLog
        async with self._sm()() as session:
            rows = (await session.execute(
                select(AiAnswerLog).where(AiAnswerLog.conversation_id == conversation_id)
            )).scalars().all()
            return [_view(r) for r in rows]

    async def sum_cost_since(self, since: datetime, *, bot_id: str | None = None) -> float:
        from sqlalchemy import func as sa_func

        from app.integrations.crm.db import AiAnswerLog
        async with self._sm()() as session:
            q = select(sa_func.coalesce(sa_func.sum(AiAnswerLog.cost), 0.0)).where(AiAnswerLog.created_at >= since)
            if bot_id is not None:
                q = q.where(AiAnswerLog.bot_id == bot_id)
            result = (await session.execute(q)).scalar()
            return float(result or 0.0)


def _view(row: Any) -> AiAnswerLogView:
    return AiAnswerLogView(
        id=row.id, request_id=row.request_id or "", generation_id=row.generation_id,
        conversation_id=row.conversation_id, lead_id=row.lead_id, bot_id=row.bot_id or "",
        model=row.model or "", prompt_version=row.prompt_version or "",
        input_tokens=row.input_tokens, output_tokens=row.output_tokens, total_tokens=row.total_tokens,
        cached_tokens=row.cached_tokens, cost=row.cost, cost_source=row.cost_source,
        latency_ms=row.latency_ms, outcome=row.outcome or "reserved", retry_count=row.retry_count or 0,
        client_message_id=row.client_message_id, bot_message_id=row.bot_message_id,
        source=row.source or "llm", knowledge_entry_ids=list(row.knowledge_entry_ids or []),
        language=row.language, intent=row.intent, confidence=row.confidence, evidence=row.evidence,
        suggested_status=row.suggested_status, applied_status=row.applied_status,
        lead_temperature=row.lead_temperature, bot_phase=row.bot_phase, dialog_owner=row.dialog_owner,
        validator_violations=list(row.validator_violations or []),
        created_at=row.created_at, updated_at=row.updated_at,
    )


_memory_ai_log_store = MemoryAiLogStore()
_pg_ai_log_store: PostgresAiLogStore | None = None


def get_ai_log_store():
    global _pg_ai_log_store
    if settings.panel_backend == "postgres":
        if _pg_ai_log_store is None:
            _pg_ai_log_store = PostgresAiLogStore()
        return _pg_ai_log_store
    return _memory_ai_log_store


def reset() -> None:
    """Сброс memory-стора (для тестов)."""
    _memory_ai_log_store._reset()

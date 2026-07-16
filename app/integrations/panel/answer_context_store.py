"""Increment 7 telegram-pilot: store for the additive `answer_context` table — the
unified feedback anchor written for EVERY automatic answer (FAQ match, LLM sent,
safe_fallback, budget_fallback, model_error_fallback, handoff_only). Does NOT replace
or touch `faq_kb_answer_log` (`app/core/faq_kb.py`) or `ai_answer_log`
(`app/integrations/panel/ai_log_store.py`) — both keep writing exactly as before; this
is the ADDITIONAL row created at each automatic-answer send point so a
`feedback_token` exists BEFORE the reply is sent (needed for the inline keyboard's
callback_data — see `app/channels/telegram.py`).

Two backends behind one contract (same convention as every other pilot store):
`MemoryAnswerContextStore` (default: tests/offline) and `PostgresAnswerContextStore`
(prod), selected by `settings.panel_backend`.

Lifecycle: `create(...)` inserts the row (mints `feedback_token`) BEFORE the reply is
sent. `attach_sent_message(...)` updates `bot_message_id`/`provider_bot_message_id`
once the channel send has completed — best-effort, a failure here must never break the
reply already sent (callers wrap it defensively, same pattern as
`app/core/ai_reply.py::_send_and_log`).

NEVER stores: API keys, the full system prompt, the webhook secret, extra PII, or the
raw BLOCKED model reply — see `AnswerContext` in `app/integrations/crm/db.py` and
`app/core/pilot_validator.py::SAFE_FALLBACK_TEXT`.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings

# faq | llm | safe_fallback | handoff | budget_fallback | model_error_fallback
SOURCES: frozenset[str] = frozenset({"faq", "llm", "safe_fallback", "handoff", "budget_fallback", "model_error_fallback"})

# faq_answered | llm_answered | safe_fallback | validator_blocked | budget_fallback |
# model_error_fallback | handoff_only — the ONLY outcomes eligible for feedback (§15).
FEEDBACK_ELIGIBLE_OUTCOMES: frozenset[str] = frozenset({
    "faq_answered", "llm_answered", "safe_fallback", "validator_blocked",
    "budget_fallback", "model_error_fallback", "handoff_only",
})

# Automatic-answer sources a callback is allowed to rate (§7 authorization) — a manual
# manager reply or a command response never gets an answer_context row in the first
# place, so this set doubles as documentation of what CAN exist here.
FEEDBACK_ELIGIBLE_SOURCES: frozenset[str] = SOURCES


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_feedback_token() -> str:
    """Short random urlsafe token (~12 chars) — opaque, unique, indexed. Never a
    sequential id or anything that leaks row count/order."""
    return secrets.token_urlsafe(9)  # 9 raw bytes -> 12 base64url chars, no padding


@dataclass
class AnswerContextView:
    id: int
    conversation_id: int | None = None
    lead_id: int | None = None
    session_id: str = ""
    bot_id: str = ""
    channel: str = "telegram"
    telegram_tester_id: str = ""
    chat_id: str = ""
    client_message_id: str | None = None
    provider_client_message_id: str | None = None
    bot_message_id: str | None = None
    provider_bot_message_id: str | None = None
    source: str = ""
    faq_entry_id: int | None = None
    faq_version_id: int | None = None
    matched_variant_id: int | None = None
    match_type: str | None = None
    match_score: float | None = None
    model: str | None = None
    prompt_version: str | None = None
    knowledge_entry_ids: list[int] = field(default_factory=list)
    language: str | None = None
    reply_text: str = ""
    intent: str | None = None
    confidence: float | None = None
    evidence: str | None = None
    suggested_status: str | None = None
    applied_status: str | None = None
    lead_temperature: str | None = None
    bot_phase: str | None = None
    dialog_owner: str | None = None
    validator_violations: list[str] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost: float | None = None
    cost_source: str | None = None
    latency_ms: float | None = None
    outcome: str = ""
    feedback_token: str = ""
    created_at: datetime | None = None

    @property
    def feedback_eligible(self) -> bool:
        return self.outcome in FEEDBACK_ELIGIBLE_OUTCOMES and bool(self.bot_message_id)


class MemoryAnswerContextStore:
    def __init__(self) -> None:
        self._rows: dict[int, AnswerContextView] = {}
        self._seq = 0

    def _reset(self) -> None:
        self.__init__()  # type: ignore[misc]

    async def create(self, **fields: Any) -> AnswerContextView:
        self._seq += 1
        fields.setdefault("feedback_token", new_feedback_token())
        row = AnswerContextView(id=self._seq, created_at=_now(), **fields)
        self._rows[row.id] = row
        return row

    async def attach_sent_message(
        self, answer_context_id: int, *, bot_message_id: str | None, provider_bot_message_id: str | None = None,
    ) -> AnswerContextView | None:
        row = self._rows.get(answer_context_id)
        if row is None:
            return None
        row.bot_message_id = bot_message_id
        row.provider_bot_message_id = provider_bot_message_id
        return row

    async def get(self, answer_context_id: int) -> AnswerContextView | None:
        return self._rows.get(answer_context_id)

    async def get_by_token(self, feedback_token: str) -> AnswerContextView | None:
        if not feedback_token:
            return None
        for row in self._rows.values():
            if row.feedback_token == feedback_token:
                return row
        return None

    async def list_for_conversation(self, conversation_id: int) -> list[AnswerContextView]:
        rows = [r for r in self._rows.values() if r.conversation_id == conversation_id]
        return sorted(rows, key=lambda r: r.id)

    async def get_latest_automatic_for_session(self, bot_id: str, session_id: str) -> AnswerContextView | None:
        """Most recent feedback-eligible answer for `(bot_id, session_id)` — used by
        `/feedback <text>` (§10) to find "the last automatic answer_context"."""
        candidates = [
            r for r in self._rows.values()
            if r.bot_id == bot_id and r.session_id == session_id and r.feedback_eligible
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.id)

    async def list_eligible(self, *, bot_id: str | None = None) -> list[AnswerContextView]:
        """All feedback-eligible rows (§15), optionally filtered by `bot_id` — backs
        `FeedbackService.get_feedback_statistics`'s "answers with no feedback" count."""
        rows = [r for r in self._rows.values() if r.feedback_eligible]
        if bot_id is not None:
            rows = [r for r in rows if r.bot_id == bot_id]
        return sorted(rows, key=lambda r: r.id)


class PostgresAnswerContextStore:
    def __init__(self, sessionmaker: async_sessionmaker | None = None) -> None:
        self._sessionmaker = sessionmaker

    def _sm(self) -> async_sessionmaker:
        if self._sessionmaker is None:
            from app.integrations.crm.db import get_sessionmaker
            self._sessionmaker = get_sessionmaker()
        return self._sessionmaker

    def sessionmaker(self) -> async_sessionmaker:
        return self._sm()

    async def create(self, **fields: Any) -> AnswerContextView:
        from app.integrations.crm.db import AnswerContext
        fields.setdefault("feedback_token", new_feedback_token())
        async with self._sm()() as session:
            row = AnswerContext(**fields)
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _view(row)

    async def attach_sent_message(
        self, answer_context_id: int, *, bot_message_id: str | None, provider_bot_message_id: str | None = None,
    ) -> AnswerContextView | None:
        from app.integrations.crm.db import AnswerContext
        async with self._sm()() as session:
            row = await session.get(AnswerContext, answer_context_id)
            if row is None:
                return None
            row.bot_message_id = bot_message_id
            row.provider_bot_message_id = provider_bot_message_id
            await session.commit()
            await session.refresh(row)
            return _view(row)

    async def get(self, answer_context_id: int) -> AnswerContextView | None:
        from app.integrations.crm.db import AnswerContext
        async with self._sm()() as session:
            row = await session.get(AnswerContext, answer_context_id)
            return _view(row) if row is not None else None

    async def get_by_token(self, feedback_token: str) -> AnswerContextView | None:
        from app.integrations.crm.db import AnswerContext
        if not feedback_token:
            return None
        async with self._sm()() as session:
            row = (await session.execute(
                select(AnswerContext).where(AnswerContext.feedback_token == feedback_token)
            )).scalar_one_or_none()
            return _view(row) if row is not None else None

    async def list_for_conversation(self, conversation_id: int) -> list[AnswerContextView]:
        from app.integrations.crm.db import AnswerContext
        async with self._sm()() as session:
            rows = (await session.execute(
                select(AnswerContext).where(AnswerContext.conversation_id == conversation_id)
                .order_by(AnswerContext.id)
            )).scalars().all()
            return [_view(r) for r in rows]

    async def get_latest_automatic_for_session(self, bot_id: str, session_id: str) -> AnswerContextView | None:
        from app.integrations.crm.db import AnswerContext
        async with self._sm()() as session:
            rows = (await session.execute(
                select(AnswerContext)
                .where(AnswerContext.bot_id == bot_id)
                .where(AnswerContext.session_id == session_id)
                .where(AnswerContext.outcome.in_(FEEDBACK_ELIGIBLE_OUTCOMES))
                .where(AnswerContext.bot_message_id.is_not(None))
                .order_by(AnswerContext.id.desc())
                .limit(1)
            )).scalars().all()
            return _view(rows[0]) if rows else None

    async def list_eligible(self, *, bot_id: str | None = None) -> list[AnswerContextView]:
        from app.integrations.crm.db import AnswerContext
        async with self._sm()() as session:
            q = (
                select(AnswerContext)
                .where(AnswerContext.outcome.in_(FEEDBACK_ELIGIBLE_OUTCOMES))
                .where(AnswerContext.bot_message_id.is_not(None))
                .order_by(AnswerContext.id)
            )
            if bot_id is not None:
                q = q.where(AnswerContext.bot_id == bot_id)
            rows = (await session.execute(q)).scalars().all()
            return [_view(r) for r in rows]


def _view(row: Any) -> AnswerContextView:
    return AnswerContextView(
        id=row.id, conversation_id=row.conversation_id, lead_id=row.lead_id, session_id=row.session_id or "",
        bot_id=row.bot_id or "", channel=row.channel or "telegram", telegram_tester_id=row.telegram_tester_id or "",
        chat_id=row.chat_id or "", client_message_id=row.client_message_id,
        provider_client_message_id=row.provider_client_message_id, bot_message_id=row.bot_message_id,
        provider_bot_message_id=row.provider_bot_message_id, source=row.source or "",
        faq_entry_id=row.faq_entry_id, faq_version_id=row.faq_version_id, matched_variant_id=row.matched_variant_id,
        match_type=row.match_type, match_score=row.match_score, model=row.model, prompt_version=row.prompt_version,
        knowledge_entry_ids=list(row.knowledge_entry_ids or []), language=row.language, reply_text=row.reply_text or "",
        intent=row.intent, confidence=row.confidence, evidence=row.evidence, suggested_status=row.suggested_status,
        applied_status=row.applied_status, lead_temperature=row.lead_temperature, bot_phase=row.bot_phase,
        dialog_owner=row.dialog_owner, validator_violations=list(row.validator_violations or []),
        input_tokens=row.input_tokens, output_tokens=row.output_tokens, total_tokens=row.total_tokens,
        cost=row.cost, cost_source=row.cost_source, latency_ms=row.latency_ms, outcome=row.outcome or "",
        feedback_token=row.feedback_token or "", created_at=row.created_at,
    )


_memory_store = MemoryAnswerContextStore()
_pg_store: PostgresAnswerContextStore | None = None


def get_answer_context_store():
    global _pg_store
    if settings.panel_backend == "postgres":
        if _pg_store is None:
            _pg_store = PostgresAnswerContextStore()
        return _pg_store
    return _memory_store


def reset() -> None:
    """Сброс memory-стора (для тестов)."""
    _memory_store._reset()

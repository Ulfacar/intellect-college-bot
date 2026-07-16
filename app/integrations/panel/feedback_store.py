"""Increment 7 telegram-pilot (+ Increment 7.1 corrective, two-axis split): store for
the additive `feedback` table — one tester's rating/comment on ONE `answer_context`
row (`app/integrations/panel/answer_context_store.py`).

`quality_rating`, `strategy_rating`, `comment` and `review_status` are all SEPARATE,
INDEPENDENT axes (§ Feedback model) — this module never conflates them, and setting
one NEVER clears another. The legacy single-axis `rating` column is kept, read-only
after the one-time backfill (`migrations/0006_feedback_two_axis.sql` /
`app/integrations/crm/db.py::backfill_feedback_rating_axes`) — new code never writes
it again. `UNIQUE(answer_context_id, telegram_tester_id)` (§8 idempotency): the FIRST
rating from a tester for a given answer (on EITHER axis) creates the row; any LATER
rating from the SAME tester for the SAME answer on the SAME axis UPDATEs that row in
place (only that axis's column + updated_at; the other axis and any comment are
preserved). Two backends behind one contract, same convention as every other pilot
store: `MemoryFeedbackStore` (default) / `PostgresFeedbackStore` (prod), selected by
`settings.panel_backend`.

Concurrency (§8/§13): Postgres relies on the real unique constraint — an insert that
races another insert for the same key gets `IntegrityError`, and the loser falls back
to an UPDATE of the row the winner just created, so two concurrent callbacks always
collapse to exactly one row. Memory relies on a per-key `asyncio.Lock` (same
single-process/sticky-session convention as `app/core/telegram_sessions.py::_locks`).
No network call happens inside either code path (§13 "no network inside the
transaction" — this store never calls OpenRouter/Telegram).

`app/core/feedback_service.py` is the ONLY caller — this module has no business
rules (authorization, audit, idempotency *decisions*) of its own, only the mechanics
of "insert or update the one allowed row for this key".
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings

# Increment 7.1: the two INDEPENDENT rating axes replacing the single Increment-7
# `rating` value. `appropriate` is brand new (no legacy source, see migration 0006).
QUALITY_RATINGS: frozenset[str] = frozenset({"correct", "inaccurate", "incorrect"})
STRATEGY_RATINGS: frozenset[str] = frozenset({
    "appropriate", "should_push", "should_not_push", "should_handoff",
})
# LEGACY (Increment 7) single-axis values — kept only to validate/interpret the old
# `rating` column (e.g. the backfill mapping); new code never writes to `rating`.
LEGACY_RATINGS: frozenset[str] = frozenset(
    QUALITY_RATINGS | (STRATEGY_RATINGS - {"appropriate"})
)
REVIEW_STATUSES: frozenset[str] = frozenset({"unreviewed", "in_review", "fixed", "dismissed"})

Axis = Literal["quality", "strategy"]
UpsertAction = Literal["created", "updated", "noop"]


def _axis_field(axis: str) -> str:
    """Maps an axis name to its `Feedback`/`FeedbackView` column — the ONLY place that
    knows this mapping, so `set_axis_rating` never touches the other axis's column."""
    if axis == "quality":
        return "quality_rating"
    if axis == "strategy":
        return "strategy_rating"
    raise ValueError(f"unknown feedback axis: {axis!r}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class FeedbackView:
    id: int
    answer_context_id: int = 0
    conversation_id: int | None = None
    lead_id: int | None = None
    session_id: str = ""
    bot_id: str = ""
    telegram_tester_id: str = ""
    rating: str | None = None  # LEGACY (Increment 7) — read-only, see module docstring
    quality_rating: str | None = None
    strategy_rating: str | None = None
    comment: str | None = None
    expected_answer: str | None = None
    expected_intent: str | None = None
    expected_status: str | None = None
    expected_handoff: bool | None = None
    review_status: str = "unreviewed"
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    resolution_note: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# --------------------------------------------------------------------------------------
# Memory backend
# --------------------------------------------------------------------------------------

class MemoryFeedbackStore:
    def __init__(self) -> None:
        self._rows: dict[int, FeedbackView] = {}
        self._seq = 0
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}

    def _reset(self) -> None:
        self.__init__()  # type: ignore[misc]

    def _lock_for(self, key: tuple[int, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks.setdefault(key, asyncio.Lock())
        return lock

    def _find(self, answer_context_id: int, telegram_tester_id: str) -> FeedbackView | None:
        for row in self._rows.values():
            if row.answer_context_id == answer_context_id and row.telegram_tester_id == telegram_tester_id:
                return row
        return None

    async def set_axis_rating(
        self, *, answer_context_id: int, telegram_tester_id: str, axis: str, value: str,
        conversation_id: int | None, lead_id: int | None, session_id: str, bot_id: str,
    ) -> tuple[FeedbackView, UpsertAction]:
        """Increment 7.1: sets ONLY the given axis's column (`quality_rating` or
        `strategy_rating`) — the other axis and any existing `comment` are always
        preserved, whether this creates the row or updates it."""
        field = _axis_field(axis)
        key = (answer_context_id, telegram_tester_id)
        async with self._lock_for(key):
            existing = self._find(answer_context_id, telegram_tester_id)
            now = _now()
            if existing is None:
                self._seq += 1
                row = FeedbackView(
                    id=self._seq, answer_context_id=answer_context_id, telegram_tester_id=telegram_tester_id,
                    conversation_id=conversation_id, lead_id=lead_id, session_id=session_id,
                    bot_id=bot_id, created_at=now, updated_at=now,
                )
                setattr(row, field, value)
                self._rows[row.id] = row
                return row, "created"
            if getattr(existing, field) == value:
                return existing, "noop"
            setattr(existing, field, value)
            existing.updated_at = now
            return existing, "updated"

    async def create_or_update_comment(
        self, *, answer_context_id: int, telegram_tester_id: str, comment: str,
        conversation_id: int | None, lead_id: int | None, session_id: str, bot_id: str,
    ) -> FeedbackView:
        key = (answer_context_id, telegram_tester_id)
        async with self._lock_for(key):
            existing = self._find(answer_context_id, telegram_tester_id)
            now = _now()
            if existing is None:
                self._seq += 1
                row = FeedbackView(
                    id=self._seq, answer_context_id=answer_context_id, telegram_tester_id=telegram_tester_id,
                    comment=comment, conversation_id=conversation_id, lead_id=lead_id, session_id=session_id,
                    bot_id=bot_id, created_at=now, updated_at=now,
                )
                self._rows[row.id] = row
                return row
            existing.comment = comment
            existing.updated_at = now
            return existing

    async def get(self, feedback_id: int) -> FeedbackView | None:
        return self._rows.get(feedback_id)

    async def get_by_answer_context_and_tester(self, answer_context_id: int, telegram_tester_id: str) -> FeedbackView | None:
        return self._find(answer_context_id, telegram_tester_id)

    async def list_for_answer_context(self, answer_context_id: int) -> list[FeedbackView]:
        return [r for r in self._rows.values() if r.answer_context_id == answer_context_id]

    async def list_all(self) -> list[FeedbackView]:
        return sorted(self._rows.values(), key=lambda r: r.id)

    async def update_review(
        self, feedback_id: int, *, review_status: str, reviewed_by: str | None = None,
        resolution_note: str | None = None,
    ) -> FeedbackView | None:
        row = self._rows.get(feedback_id)
        if row is None:
            return None
        row.review_status = review_status
        row.reviewed_by = reviewed_by
        row.reviewed_at = _now()
        if resolution_note is not None:
            row.resolution_note = resolution_note
        row.updated_at = _now()
        return row

    async def set_expected_correction(
        self, feedback_id: int, *, expected_answer: str | None = None, expected_intent: str | None = None,
        expected_status: str | None = None, expected_handoff: bool | None = None,
    ) -> FeedbackView | None:
        row = self._rows.get(feedback_id)
        if row is None:
            return None
        if expected_answer is not None:
            row.expected_answer = expected_answer
        if expected_intent is not None:
            row.expected_intent = expected_intent
        if expected_status is not None:
            row.expected_status = expected_status
        if expected_handoff is not None:
            row.expected_handoff = expected_handoff
        row.updated_at = _now()
        return row


# --------------------------------------------------------------------------------------
# Postgres backend
# --------------------------------------------------------------------------------------

class PostgresFeedbackStore:
    def __init__(self, sessionmaker: async_sessionmaker | None = None) -> None:
        self._sessionmaker = sessionmaker

    def _sm(self) -> async_sessionmaker:
        if self._sessionmaker is None:
            from app.integrations.crm.db import get_sessionmaker
            self._sessionmaker = get_sessionmaker()
        return self._sessionmaker

    def sessionmaker(self) -> async_sessionmaker:
        return self._sm()

    async def set_axis_rating(
        self, *, answer_context_id: int, telegram_tester_id: str, axis: str, value: str,
        conversation_id: int | None, lead_id: int | None, session_id: str, bot_id: str,
    ) -> tuple[FeedbackView, UpsertAction]:
        """Increment 7.1: sets ONLY the given axis's column — mirrors
        `MemoryFeedbackStore.set_axis_rating`, see its docstring."""
        from app.integrations.crm.db import Feedback
        field = _axis_field(axis)
        try:
            async with self._sm()() as session:
                async with session.begin():
                    row = Feedback(
                        answer_context_id=answer_context_id, telegram_tester_id=telegram_tester_id,
                        conversation_id=conversation_id, lead_id=lead_id,
                        session_id=session_id, bot_id=bot_id,
                    )
                    setattr(row, field, value)
                    session.add(row)
                    await session.flush()
                await session.refresh(row)
                return _view(row), "created"
        except IntegrityError:
            pass  # race (or a pre-existing row) — fall through to update below

        async with self._sm()() as session:
            async with session.begin():
                existing = (await session.execute(
                    select(Feedback)
                    .where(Feedback.answer_context_id == answer_context_id)
                    .where(Feedback.telegram_tester_id == telegram_tester_id)
                )).scalar_one_or_none()
                if existing is None:  # pragma: no cover — extremely unlikely race
                    raise RuntimeError("feedback upsert race: row disappeared")
                if getattr(existing, field) == value:
                    return _view(existing), "noop"
                setattr(existing, field, value)
                await session.flush()
            await session.refresh(existing)
            return _view(existing), "updated"

    async def create_or_update_comment(
        self, *, answer_context_id: int, telegram_tester_id: str, comment: str,
        conversation_id: int | None, lead_id: int | None, session_id: str, bot_id: str,
    ) -> FeedbackView:
        from app.integrations.crm.db import Feedback
        try:
            async with self._sm()() as session:
                async with session.begin():
                    row = Feedback(
                        answer_context_id=answer_context_id, telegram_tester_id=telegram_tester_id,
                        comment=comment, conversation_id=conversation_id, lead_id=lead_id,
                        session_id=session_id, bot_id=bot_id,
                    )
                    session.add(row)
                    await session.flush()
                await session.refresh(row)
                return _view(row)
        except IntegrityError:
            pass

        async with self._sm()() as session:
            async with session.begin():
                existing = (await session.execute(
                    select(Feedback)
                    .where(Feedback.answer_context_id == answer_context_id)
                    .where(Feedback.telegram_tester_id == telegram_tester_id)
                )).scalar_one_or_none()
                if existing is None:  # pragma: no cover
                    raise RuntimeError("feedback upsert race: row disappeared")
                existing.comment = comment
                await session.flush()
            await session.refresh(existing)
            return _view(existing)

    async def get(self, feedback_id: int) -> FeedbackView | None:
        from app.integrations.crm.db import Feedback
        async with self._sm()() as session:
            row = await session.get(Feedback, feedback_id)
            return _view(row) if row is not None else None

    async def get_by_answer_context_and_tester(self, answer_context_id: int, telegram_tester_id: str) -> FeedbackView | None:
        from app.integrations.crm.db import Feedback
        async with self._sm()() as session:
            row = (await session.execute(
                select(Feedback)
                .where(Feedback.answer_context_id == answer_context_id)
                .where(Feedback.telegram_tester_id == telegram_tester_id)
            )).scalar_one_or_none()
            return _view(row) if row is not None else None

    async def list_for_answer_context(self, answer_context_id: int) -> list[FeedbackView]:
        from app.integrations.crm.db import Feedback
        async with self._sm()() as session:
            rows = (await session.execute(
                select(Feedback).where(Feedback.answer_context_id == answer_context_id)
            )).scalars().all()
            return [_view(r) for r in rows]

    async def list_all(self) -> list[FeedbackView]:
        from app.integrations.crm.db import Feedback
        async with self._sm()() as session:
            rows = (await session.execute(select(Feedback).order_by(Feedback.id))).scalars().all()
            return [_view(r) for r in rows]

    async def update_review(
        self, feedback_id: int, *, review_status: str, reviewed_by: str | None = None,
        resolution_note: str | None = None,
    ) -> FeedbackView | None:
        from app.integrations.crm.db import Feedback
        async with self._sm()() as session:
            row = await session.get(Feedback, feedback_id)
            if row is None:
                return None
            row.review_status = review_status
            row.reviewed_by = reviewed_by
            row.reviewed_at = _now()
            if resolution_note is not None:
                row.resolution_note = resolution_note
            await session.commit()
            await session.refresh(row)
            return _view(row)

    async def set_expected_correction(
        self, feedback_id: int, *, expected_answer: str | None = None, expected_intent: str | None = None,
        expected_status: str | None = None, expected_handoff: bool | None = None,
    ) -> FeedbackView | None:
        from app.integrations.crm.db import Feedback
        async with self._sm()() as session:
            row = await session.get(Feedback, feedback_id)
            if row is None:
                return None
            if expected_answer is not None:
                row.expected_answer = expected_answer
            if expected_intent is not None:
                row.expected_intent = expected_intent
            if expected_status is not None:
                row.expected_status = expected_status
            if expected_handoff is not None:
                row.expected_handoff = expected_handoff
            await session.commit()
            await session.refresh(row)
            return _view(row)


def _view(row: Any) -> FeedbackView:
    return FeedbackView(
        id=row.id, answer_context_id=row.answer_context_id, conversation_id=row.conversation_id,
        lead_id=row.lead_id, session_id=row.session_id or "", bot_id=row.bot_id or "",
        telegram_tester_id=row.telegram_tester_id or "", rating=row.rating,
        quality_rating=row.quality_rating, strategy_rating=row.strategy_rating, comment=row.comment,
        expected_answer=row.expected_answer, expected_intent=row.expected_intent,
        expected_status=row.expected_status, expected_handoff=row.expected_handoff,
        review_status=row.review_status or "unreviewed", reviewed_by=row.reviewed_by,
        reviewed_at=row.reviewed_at, resolution_note=row.resolution_note,
        created_at=row.created_at, updated_at=row.updated_at,
    )


_memory_store = MemoryFeedbackStore()
_pg_store: PostgresFeedbackStore | None = None


def get_feedback_store():
    global _pg_store
    if settings.panel_backend == "postgres":
        if _pg_store is None:
            _pg_store = PostgresFeedbackStore()
        return _pg_store
    return _memory_store


def reset() -> None:
    """Сброс memory-стора (для тестов)."""
    _memory_store._reset()

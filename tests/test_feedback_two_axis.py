"""Increment 7.1 corrective: two INDEPENDENT feedback axes — `quality_rating` (was the
CONTENT of this answer correct?) and `strategy_rating` (was the conversation-handling
APPROACH right?), replacing the single Increment-7 `rating`. Covers the brief's §9
scenario list not already exercised by the updated Increment-7 test files:
  1. quality rating saved independently
  2. strategy rating saved independently
  3. both axes coexist on the same Feedback row
  4. changing quality keeps strategy
  5. changing strategy keeps quality
  6. changing either axis keeps the comment
  7. repeated identical callback never creates a duplicate row (per axis)
  8. legacy `rating` backfills to `quality_rating`
  9. legacy `rating` backfills to `strategy_rating`
 13. a quality-axis callback never calls FAQ/LLM
 14. a strategy-axis callback never calls FAQ/LLM
 15. authorization + session/bot isolation still holds with the NEW q_/s_ codes
 16. store-level axis writes are parametrized over memory + Postgres(SQLite StaticPool)

(Filter tests 10/11 and the split-statistics test 12 live in
tests/test_feedback_review_backend.py next to the other list_feedback/statistics
tests; #17/#18 are verified by the full `pytest -q` run, not a single test here.)
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core import telegram_sessions
from app.core.feedback_service import FeedbackService
from app.integrations.crm.db import backfill_feedback_rating_axes, init_models
from app.integrations.panel.answer_context_store import get_answer_context_store, reset as reset_ctx
from app.integrations.panel.audit_store import get_audit_store
from app.integrations.panel.feedback_store import (
    MemoryFeedbackStore,
    PostgresFeedbackStore,
    get_feedback_store,
    reset as reset_fb,
)

BACKENDS = ["memory", "postgres"]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    reset_ctx()
    reset_fb()
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [])
    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", [])
    yield
    reset_ctx()
    reset_fb()


class _RecordingAdapter:
    channel = "telegram"

    def __init__(self):
        self.sent: list[tuple[str, str, dict | None]] = []
        self.acks: list[tuple[str, str, bool]] = []

    async def send(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((chat_id, text, reply_markup))
        return None

    async def answer_callback(self, callback_query_id, text="", *, show_alert=False):
        self.acks.append((callback_query_id, text, show_alert))


async def _sent_context(bot_id: str, tester_id: str):
    session = await telegram_sessions.ensure_active_session(bot_id, tester_id, external_chat_id=tester_id)
    ctx = await get_answer_context_store().create(
        conversation_id=session.conversation.id, lead_id=session.lead.id,
        session_id=session.conversation.session_id, bot_id=bot_id, channel="telegram",
        telegram_tester_id=tester_id, chat_id=tester_id, source="faq", outcome="faq_answered", reply_text="x",
    )
    await get_answer_context_store().attach_sent_message(ctx.id, bot_message_id="1", provider_bot_message_id="1")
    return await get_answer_context_store().get(ctx.id)


async def _pg_feedback_store() -> PostgresFeedbackStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    await init_models(engine)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    return PostgresFeedbackStore(sessionmaker=sm)


async def _fb_store(kind: str):
    return MemoryFeedbackStore() if kind == "memory" else await _pg_feedback_store()


# --------------------------------------------------------------------------------------
# 1/2/16. quality and strategy ratings saved independently, parametrized memory+postgres
# (store-level — the SAME assertions hold for both backends, satisfying scenario 16).
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_quality_rating_saved_independently(backend):
    async def scenario():
        store = await _fb_store(backend)
        view, action = await store.set_axis_rating(
            answer_context_id=1, telegram_tester_id="t1", axis="quality", value="correct",
            conversation_id=1, lead_id=1, session_id="s1", bot_id="b1",
        )
        assert action == "created"
        assert view.quality_rating == "correct"
        assert view.strategy_rating is None
        assert view.rating is None  # legacy column never written by new code
    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_strategy_rating_saved_independently(backend):
    async def scenario():
        store = await _fb_store(backend)
        view, action = await store.set_axis_rating(
            answer_context_id=1, telegram_tester_id="t1", axis="strategy", value="appropriate",
            conversation_id=1, lead_id=1, session_id="s1", bot_id="b1",
        )
        assert action == "created"
        assert view.strategy_rating == "appropriate"
        assert view.quality_rating is None
        assert view.rating is None
    _run(scenario())


# --------------------------------------------------------------------------------------
# 3. Both axes coexist on the SAME Feedback row (one row per answer_context+tester,
# not two).
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_quality_and_strategy_coexist_on_same_row(backend):
    async def scenario():
        store = await _fb_store(backend)
        v1, a1 = await store.set_axis_rating(
            answer_context_id=1, telegram_tester_id="t1", axis="quality", value="incorrect",
            conversation_id=1, lead_id=1, session_id="s1", bot_id="b1",
        )
        v2, a2 = await store.set_axis_rating(
            answer_context_id=1, telegram_tester_id="t1", axis="strategy", value="should_push",
            conversation_id=1, lead_id=1, session_id="s1", bot_id="b1",
        )
        assert a1 == "created"
        assert a2 == "updated"  # same row, second axis set on it
        assert v1.id == v2.id
        rows = await store.list_all()
        assert len(rows) == 1
        assert rows[0].quality_rating == "incorrect"
        assert rows[0].strategy_rating == "should_push"
    _run(scenario())


# --------------------------------------------------------------------------------------
# 4/5. Changing ONE axis never touches the other.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_changing_quality_keeps_strategy(backend):
    async def scenario():
        store = await _fb_store(backend)
        await store.set_axis_rating(
            answer_context_id=1, telegram_tester_id="t1", axis="strategy", value="should_handoff",
            conversation_id=1, lead_id=1, session_id="s1", bot_id="b1",
        )
        await store.set_axis_rating(
            answer_context_id=1, telegram_tester_id="t1", axis="quality", value="correct",
            conversation_id=1, lead_id=1, session_id="s1", bot_id="b1",
        )
        view, action = await store.set_axis_rating(
            answer_context_id=1, telegram_tester_id="t1", axis="quality", value="inaccurate",
            conversation_id=1, lead_id=1, session_id="s1", bot_id="b1",
        )
        assert action == "updated"
        assert view.quality_rating == "inaccurate"
        assert view.strategy_rating == "should_handoff"  # untouched
    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_changing_strategy_keeps_quality(backend):
    async def scenario():
        store = await _fb_store(backend)
        await store.set_axis_rating(
            answer_context_id=1, telegram_tester_id="t1", axis="quality", value="correct",
            conversation_id=1, lead_id=1, session_id="s1", bot_id="b1",
        )
        await store.set_axis_rating(
            answer_context_id=1, telegram_tester_id="t1", axis="strategy", value="should_push",
            conversation_id=1, lead_id=1, session_id="s1", bot_id="b1",
        )
        view, action = await store.set_axis_rating(
            answer_context_id=1, telegram_tester_id="t1", axis="strategy", value="should_not_push",
            conversation_id=1, lead_id=1, session_id="s1", bot_id="b1",
        )
        assert action == "updated"
        assert view.strategy_rating == "should_not_push"
        assert view.quality_rating == "correct"  # untouched
    _run(scenario())


# --------------------------------------------------------------------------------------
# 6. Changing EITHER axis keeps an existing comment.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_changing_either_axis_keeps_comment(backend):
    async def scenario():
        store = await _fb_store(backend)
        await store.set_axis_rating(
            answer_context_id=1, telegram_tester_id="t1", axis="quality", value="correct",
            conversation_id=1, lead_id=1, session_id="s1", bot_id="b1",
        )
        await store.create_or_update_comment(
            answer_context_id=1, telegram_tester_id="t1", comment="есть нюанс",
            conversation_id=1, lead_id=1, session_id="s1", bot_id="b1",
        )
        # change quality -> comment survives
        v1, _ = await store.set_axis_rating(
            answer_context_id=1, telegram_tester_id="t1", axis="quality", value="incorrect",
            conversation_id=1, lead_id=1, session_id="s1", bot_id="b1",
        )
        assert v1.comment == "есть нюанс"
        # change strategy (first time on this row) -> comment still survives
        v2, _ = await store.set_axis_rating(
            answer_context_id=1, telegram_tester_id="t1", axis="strategy", value="appropriate",
            conversation_id=1, lead_id=1, session_id="s1", bot_id="b1",
        )
        assert v2.comment == "есть нюанс"
        rows = await store.list_all()
        assert len(rows) == 1
    _run(scenario())


# --------------------------------------------------------------------------------------
# 7. Repeated identical callback (same axis, same value) never creates a duplicate row
# — service level, through the real callback path, for BOTH axes.
# --------------------------------------------------------------------------------------

def test_repeat_callback_same_axis_creates_no_duplicate_row(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [7001])
        ctx = await _sent_context("twoaxis7", "7001")
        adapter = _RecordingAdapter()
        svc = FeedbackService()
        for code in ("s_push", "s_push", "s_push"):
            await svc.handle_callback(
                bot_id="twoaxis7", adapter=adapter, callback_query_id=f"cb-{code}-{len(adapter.acks)}",
                tester_id=7001, chat_id=7001, data=f"fb:{ctx.feedback_token}:{code}",
            )
        rows = await get_feedback_store().list_all()
        assert len(rows) == 1
        assert rows[0].strategy_rating == "should_push"
        assert len(adapter.acks) == 3  # every callback still acked (spinner clears)
        expected_ack = (
            "Ведение диалога сохранено: Надо было дожать. "
            "Оценка качества ответа — отдельно, она не сбрасывается."
        )
        assert all(text == expected_ack for _cid, text, _alert in adapter.acks)
    _run(scenario())


# --------------------------------------------------------------------------------------
# 8/9. Legacy `rating` backfills onto the correct NEW axis (idempotent, re-runnable) —
# exercises the SAME logic as migrations/0006_feedback_two_axis.sql's two UPDATEs.
# --------------------------------------------------------------------------------------

def test_legacy_rating_backfills_to_quality_axis():
    async def scenario():
        from app.integrations.crm.db import Feedback

        engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        await init_models(engine)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as session:
            session.add(Feedback(answer_context_id=1, telegram_tester_id="legacy1", rating="inaccurate"))
            await session.commit()

        async with engine.begin() as conn:
            await backfill_feedback_rating_axes(conn)

        async with sm() as session:
            row = (await session.execute(
                select(Feedback).where(Feedback.telegram_tester_id == "legacy1")
            )).scalar_one()
            assert row.rating == "inaccurate"  # legacy value untouched
            assert row.quality_rating == "inaccurate"  # backfilled
            assert row.strategy_rating is None  # no legacy source for this value
    _run(scenario())


def test_legacy_rating_backfills_to_strategy_axis():
    async def scenario():
        from app.integrations.crm.db import Feedback

        engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        await init_models(engine)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as session:
            session.add(Feedback(answer_context_id=2, telegram_tester_id="legacy2", rating="should_not_push"))
            await session.commit()

        async with engine.begin() as conn:
            await backfill_feedback_rating_axes(conn)

        async with sm() as session:
            row = (await session.execute(
                select(Feedback).where(Feedback.telegram_tester_id == "legacy2")
            )).scalar_one()
            assert row.rating == "should_not_push"
            assert row.strategy_rating == "should_not_push"  # backfilled
            assert row.quality_rating is None

        # idempotent: a tester's real tap on the NEW "appropriate" value (no legacy
        # source) must NOT be clobbered by re-running the backfill.
        async with sm() as session:
            row = (await session.execute(
                select(Feedback).where(Feedback.telegram_tester_id == "legacy2")
            )).scalar_one()
            row.strategy_rating = "appropriate"
            await session.commit()
        async with engine.begin() as conn:
            await backfill_feedback_rating_axes(conn)
        async with sm() as session:
            row = (await session.execute(
                select(Feedback).where(Feedback.telegram_tester_id == "legacy2")
            )).scalar_one()
            assert row.strategy_rating == "appropriate"  # NOT overwritten back to should_not_push
    _run(scenario())


# --------------------------------------------------------------------------------------
# 13/14. A quality- or strategy-axis callback never calls FAQ/LLM (mirrors the existing
# comment-flow "never reaches FAQ/LLM" pattern, tests/test_feedback_comment_flow.py).
# --------------------------------------------------------------------------------------

def test_quality_callback_never_calls_faq_or_llm(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [1301])
        ctx = await _sent_context("twoaxis13", "1301")
        adapter = _RecordingAdapter()
        await FeedbackService().handle_callback(
            bot_id="twoaxis13", adapter=adapter, callback_query_id="cb", tester_id=1301, chat_id=1301,
            data=f"fb:{ctx.feedback_token}:q_bad",
        )
        # handle_callback only ever calls adapter.answer_callback (and, for the
        # comment code only, adapter.send) — for a rating code, `sent` stays empty.
        assert adapter.sent == []
        assert adapter.acks[-1][1] == (
            "Качество ответа сохранено: Неправильно. "
            "Оценка ведения диалога — отдельно, она не сбрасывается."
        )
    _run(scenario())


def test_strategy_callback_never_calls_faq_or_llm(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [1401])
        ctx = await _sent_context("twoaxis14", "1401")
        adapter = _RecordingAdapter()
        await FeedbackService().handle_callback(
            bot_id="twoaxis14", adapter=adapter, callback_query_id="cb", tester_id=1401, chat_id=1401,
            data=f"fb:{ctx.feedback_token}:s_mgr",
        )
        assert adapter.sent == []
        assert adapter.acks[-1][1] == (
            "Ведение диалога сохранено: Нужен был менеджер. "
            "Оценка качества ответа — отдельно, она не сбрасывается."
        )
    _run(scenario())


# --------------------------------------------------------------------------------------
# 15. Authorization + session/bot isolation still hold with the NEW q_/s_ codes — the
# code->axis lookup happens strictly AFTER every §7 authorization check (unchanged
# order), so a rejection never even reaches CODE_TO_AXIS_VALUE.
# --------------------------------------------------------------------------------------

def test_authorization_and_isolation_preserved_with_new_axis_codes(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [1501, 1502])
        ctx = await _sent_context("twoaxis15", "1501")
        adapter = _RecordingAdapter()

        # a DIFFERENT tester (1502) tries to rate tester 1501's answer with a NEW code.
        await FeedbackService().handle_callback(
            bot_id="twoaxis15", adapter=adapter, callback_query_id="cb", tester_id=1502, chat_id=1502,
            data=f"fb:{ctx.feedback_token}:s_appr",
        )
        assert await get_feedback_store().list_all() == []
        rejected = await get_audit_store().list_by_event_type("feedback_callback_rejected")
        assert len(rejected) >= 1
        assert rejected[-1].reason == "tester_mismatch"

        # an unknown code (defensive — should never happen via a real keyboard) is
        # rejected too, AFTER authorization passes for the real owner.
        adapter2 = _RecordingAdapter()
        await FeedbackService().handle_callback(
            bot_id="twoaxis15", adapter=adapter2, callback_query_id="cb2", tester_id=1501, chat_id=1501,
            data=f"fb:{ctx.feedback_token}:q_totally_unknown",
        )
        assert await get_feedback_store().list_all() == []
        rejected2 = await get_audit_store().list_by_event_type("feedback_callback_rejected")
        assert rejected2[-1].reason == "unknown_code"
    _run(scenario())

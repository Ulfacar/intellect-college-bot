"""Increment 7: idempotency (brief scenarios 27-31, §8). `UNIQUE(answer_context_id,
telegram_tester_id)` — first rating creates the row; a later rating from the SAME
tester on the SAME answer UPDATEs it in place (keeping any existing comment); the same
rating pressed twice is a no-op; concurrent callbacks collapse to exactly one row."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core import telegram_sessions
from app.core.feedback_service import FeedbackService
from app.integrations.crm.db import init_models
from app.integrations.panel.answer_context_store import get_answer_context_store, reset as reset_ctx
from app.integrations.panel.audit_store import get_audit_store
from app.integrations.panel.feedback_store import MemoryFeedbackStore, PostgresFeedbackStore, get_feedback_store, reset as reset_fb

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
        self.acks: list[tuple[str, str, bool]] = []

    async def send(self, chat_id, text, reply_markup=None, **kw):
        return None

    async def answer_callback(self, callback_query_id, text="", *, show_alert=False):
        self.acks.append((callback_query_id, text, show_alert))


async def _pg_feedback_store() -> PostgresFeedbackStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    await init_models(engine)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    return PostgresFeedbackStore(sessionmaker=sm)


async def _fb_store(kind: str):
    return MemoryFeedbackStore() if kind == "memory" else await _pg_feedback_store()


async def _sent_context(bot_id: str, tester_id: str):
    session = await telegram_sessions.ensure_active_session(bot_id, tester_id, external_chat_id=tester_id)
    ctx = await get_answer_context_store().create(
        conversation_id=session.conversation.id, lead_id=session.lead.id,
        session_id=session.conversation.session_id, bot_id=bot_id, channel="telegram",
        telegram_tester_id=tester_id, chat_id=tester_id, source="faq", outcome="faq_answered", reply_text="x",
    )
    await get_answer_context_store().attach_sent_message(ctx.id, bot_message_id="1", provider_bot_message_id="1")
    return await get_answer_context_store().get(ctx.id)


# 27. First rating creates a Feedback row, acked "Оценка сохранена."
def test_first_rating_creates_and_acks_saved(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [2701])
        ctx = await _sent_context("idem1", "2701")
        adapter = _RecordingAdapter()
        await FeedbackService().handle_callback(
            bot_id="idem1", adapter=adapter, callback_query_id="cb", tester_id=2701, chat_id=2701,
            data=f"fb:{ctx.feedback_token}:ok",
        )
        rows = await get_feedback_store().list_all()
        assert len(rows) == 1
        assert rows[0].rating == "correct"
        assert adapter.acks[-1][1] == "Оценка сохранена: Правильно."
        lead_audit = await get_audit_store().list_for_lead(ctx.lead_id)
        recorded = [a for a in lead_audit if a.event_type == "feedback_recorded"]
        assert len(recorded) == 1
    _run(scenario())


# 28. Re-rating (different value) UPDATEs the SAME row, keeps an existing comment,
# acked "Оценка обновлена."
def test_re_rating_updates_keeps_comment_and_acks_updated(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [2801])
        ctx = await _sent_context("idem2", "2801")
        adapter = _RecordingAdapter()
        svc = FeedbackService()
        await svc.handle_callback(
            bot_id="idem2", adapter=adapter, callback_query_id="cb1", tester_id=2801, chat_id=2801,
            data=f"fb:{ctx.feedback_token}:ok",
        )
        # tester also leaves a comment via the pending-comment flow
        await get_feedback_store().create_or_update_comment(
            answer_context_id=ctx.id, telegram_tester_id="2801", comment="было неточно про сроки",
            conversation_id=ctx.conversation_id, lead_id=ctx.lead_id, session_id=ctx.session_id, bot_id="idem2",
        )
        await svc.handle_callback(
            bot_id="idem2", adapter=adapter, callback_query_id="cb2", tester_id=2801, chat_id=2801,
            data=f"fb:{ctx.feedback_token}:inacc",
        )
        rows = await get_feedback_store().list_all()
        assert len(rows) == 1  # still exactly one row
        assert rows[0].rating == "inaccurate"
        assert rows[0].comment == "было неточно про сроки"  # preserved
        assert adapter.acks[-1][1] == "Оценка заменена — по ответу учитывается только одна оценка: Неточно."
        lead_audit = await get_audit_store().list_for_lead(ctx.lead_id)
        changed = [a for a in lead_audit if a.event_type == "feedback_rating_changed"]
        assert len(changed) == 1
    _run(scenario())


# 29. Same rating pressed twice -> no-op (one row, no spurious rating-changed audit),
# but the callback is still acked (spinner must clear).
def test_same_rating_twice_is_noop(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [2901])
        ctx = await _sent_context("idem3", "2901")
        adapter = _RecordingAdapter()
        svc = FeedbackService()
        for cb_id in ("cb1", "cb2"):
            await svc.handle_callback(
                bot_id="idem3", adapter=adapter, callback_query_id=cb_id, tester_id=2901, chat_id=2901,
                data=f"fb:{ctx.feedback_token}:ok",
            )
        rows = await get_feedback_store().list_all()
        assert len(rows) == 1
        assert len(adapter.acks) == 2  # both callbacks acked
        lead_audit = await get_audit_store().list_for_lead(ctx.lead_id)
        changed = [a for a in lead_audit if a.event_type == "feedback_rating_changed"]
        assert changed == []  # second press changed nothing
    _run(scenario())


# 30. Two concurrent callbacks for the SAME (answer_context, tester) collapse to
# exactly one Feedback row. Memory: genuine `asyncio.gather` concurrency, backed by
# the per-key `asyncio.Lock` (process-lock convention, see module docstring). Postgres:
# the SAME two calls run back-to-back against the real unique-constraint-backed
# insert-then-fallback-to-update code path (`create_or_update_rating`) — true
# multi-connection concurrency isn't exercised here (the SQLite stand-in is a single
# shared connection and doesn't model overlapping transactions faithfully), but the
# collapse-to-one-row INVARIANT the constraint guarantees is the same one asserted.
@pytest.mark.parametrize("backend", BACKENDS)
def test_concurrent_rating_calls_collapse_to_one_row(backend):
    async def scenario():
        store = await _fb_store(backend)
        if backend == "memory":
            results = await asyncio.gather(
                store.create_or_update_rating(
                    answer_context_id=1, telegram_tester_id="t1", rating="correct",
                    conversation_id=1, lead_id=1, session_id="s1", bot_id="b1",
                ),
                store.create_or_update_rating(
                    answer_context_id=1, telegram_tester_id="t1", rating="inaccurate",
                    conversation_id=1, lead_id=1, session_id="s1", bot_id="b1",
                ),
            )
        else:
            results = [
                await store.create_or_update_rating(
                    answer_context_id=1, telegram_tester_id="t1", rating="correct",
                    conversation_id=1, lead_id=1, session_id="s1", bot_id="b1",
                ),
                await store.create_or_update_rating(
                    answer_context_id=1, telegram_tester_id="t1", rating="inaccurate",
                    conversation_id=1, lead_id=1, session_id="s1", bot_id="b1",
                ),
            ]
        all_rows = await store.list_all()
        assert len(all_rows) == 1
        actions = {r[1] for r in results}
        assert "created" in actions  # exactly one insert happened
    _run(scenario())


# 31. UNIQUE(answer_context_id, telegram_tester_id) is a REAL DB constraint (not just
# app-level convention) — a raw second INSERT for the same key is rejected.
def test_unique_constraint_rejects_raw_duplicate_insert():
    async def scenario():
        from app.integrations.crm.db import Feedback

        engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        await init_models(engine)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as session:
            session.add(Feedback(answer_context_id=5, telegram_tester_id="t5", rating="correct"))
            await session.commit()
        with pytest.raises(IntegrityError):
            async with sm() as session:
                session.add(Feedback(answer_context_id=5, telegram_tester_id="t5", rating="incorrect"))
                await session.commit()
    _run(scenario())

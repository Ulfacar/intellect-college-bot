"""Increment 7: app/integrations/panel/answer_context_store.py contract tests (scenarios
1-8 of the brief) — parametrized over MemoryAnswerContextStore and
PostgresAnswerContextStore on SQLite-in-memory (StaticPool, no real network Postgres —
same convention as tests/test_ai_log_store.py/test_leadstore.py)."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.integrations.crm.db import init_models
from app.integrations.panel.answer_context_store import (
    FEEDBACK_ELIGIBLE_OUTCOMES,
    MemoryAnswerContextStore,
    PostgresAnswerContextStore,
    new_feedback_token,
)

BACKENDS = ["memory", "postgres"]


async def _pg_store() -> PostgresAnswerContextStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    await init_models(engine)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    return PostgresAnswerContextStore(sessionmaker=sm)


async def _store(kind: str):
    return MemoryAnswerContextStore() if kind == "memory" else await _pg_store()


def _run(coro):
    return asyncio.run(coro)


# 1. create() mints a unique feedback_token BEFORE the reply is sent.
@pytest.mark.parametrize("backend", BACKENDS)
def test_create_mints_feedback_token(backend):
    async def scenario():
        store = await _store(backend)
        row = await store.create(
            conversation_id=1, lead_id=1, session_id="s1", bot_id="college_1", channel="telegram",
            telegram_tester_id="111", chat_id="111", source="faq", outcome="faq_answered",
            reply_text="6500$/год",
        )
        assert row.id is not None
        assert row.feedback_token
        assert len(row.feedback_token) >= 8
        assert row.bot_message_id is None  # not attached yet
    _run(scenario())


# 2. feedback_token is unique across rows.
@pytest.mark.parametrize("backend", BACKENDS)
def test_feedback_token_unique(backend):
    async def scenario():
        store = await _store(backend)
        tokens = set()
        for _ in range(20):
            row = await store.create(
                conversation_id=1, lead_id=1, session_id="s1", bot_id="college_1", channel="telegram",
                telegram_tester_id="111", chat_id="111", source="faq", outcome="faq_answered", reply_text="x",
            )
            tokens.add(row.feedback_token)
        assert len(tokens) == 20
    _run(scenario())


def test_new_feedback_token_is_short_and_urlsafe():
    token = new_feedback_token()
    assert 8 <= len(token) <= 16
    assert all(c.isalnum() or c in "-_" for c in token)


# 3. get_by_token resolves a row; unknown token -> None.
@pytest.mark.parametrize("backend", BACKENDS)
def test_get_by_token(backend):
    async def scenario():
        store = await _store(backend)
        row = await store.create(
            conversation_id=1, lead_id=1, session_id="s1", bot_id="college_1", channel="telegram",
            telegram_tester_id="111", chat_id="111", source="llm", outcome="llm_answered", reply_text="ok",
        )
        fetched = await store.get_by_token(row.feedback_token)
        assert fetched is not None
        assert fetched.id == row.id
        assert await store.get_by_token("does-not-exist") is None
        assert await store.get_by_token("") is None
    _run(scenario())


# 4. attach_sent_message updates bot_message_id/provider_bot_message_id in place.
@pytest.mark.parametrize("backend", BACKENDS)
def test_attach_sent_message(backend):
    async def scenario():
        store = await _store(backend)
        row = await store.create(
            conversation_id=1, lead_id=1, session_id="s1", bot_id="college_1", channel="telegram",
            telegram_tester_id="111", chat_id="111", source="faq", outcome="faq_answered", reply_text="x",
        )
        updated = await store.attach_sent_message(row.id, bot_message_id="42", provider_bot_message_id="999")
        assert updated.bot_message_id == "42"
        assert updated.provider_bot_message_id == "999"
        assert await store.attach_sent_message(999999, bot_message_id="1") is None
    _run(scenario())


# 5. list_for_conversation returns only rows for that conversation, in creation order.
@pytest.mark.parametrize("backend", BACKENDS)
def test_list_for_conversation(backend):
    async def scenario():
        store = await _store(backend)
        await store.create(conversation_id=1, lead_id=1, session_id="s1", bot_id="b", channel="telegram",
                            telegram_tester_id="1", chat_id="1", source="faq", outcome="faq_answered", reply_text="a")
        await store.create(conversation_id=1, lead_id=1, session_id="s1", bot_id="b", channel="telegram",
                            telegram_tester_id="1", chat_id="1", source="llm", outcome="llm_answered", reply_text="b")
        await store.create(conversation_id=2, lead_id=1, session_id="s2", bot_id="b", channel="telegram",
                            telegram_tester_id="1", chat_id="1", source="llm", outcome="llm_answered", reply_text="c")
        rows = await store.list_for_conversation(1)
        assert len(rows) == 2
        assert [r.reply_text for r in rows] == ["a", "b"]
    _run(scenario())


# 6. get_latest_automatic_for_session picks the most recent FEEDBACK-ELIGIBLE row
#    (bot_message_id set, outcome eligible) for (bot_id, session_id) — used by /feedback.
@pytest.mark.parametrize("backend", BACKENDS)
def test_get_latest_automatic_for_session_only_eligible_and_sent(backend):
    async def scenario():
        store = await _store(backend)
        row1 = await store.create(
            conversation_id=1, lead_id=1, session_id="sess-a", bot_id="college_1", channel="telegram",
            telegram_tester_id="1", chat_id="1", source="faq", outcome="faq_answered", reply_text="first",
        )
        await store.attach_sent_message(row1.id, bot_message_id="1")
        # A reserved-but-not-sent row (e.g. cancelled_by_takeover) must NOT win.
        await store.create(
            conversation_id=1, lead_id=1, session_id="sess-a", bot_id="college_1", channel="telegram",
            telegram_tester_id="1", chat_id="1", source="llm", outcome="cancelled_by_takeover", reply_text="",
        )
        row3 = await store.create(
            conversation_id=1, lead_id=1, session_id="sess-a", bot_id="college_1", channel="telegram",
            telegram_tester_id="1", chat_id="1", source="llm", outcome="llm_answered", reply_text="latest",
        )
        await store.attach_sent_message(row3.id, bot_message_id="3")

        latest = await store.get_latest_automatic_for_session("college_1", "sess-a")
        assert latest is not None
        assert latest.id == row3.id

        assert await store.get_latest_automatic_for_session("college_1", "no-such-session") is None
    _run(scenario())


# 7. list_eligible filters to feedback-eligible rows only, optionally by bot_id.
@pytest.mark.parametrize("backend", BACKENDS)
def test_list_eligible_filters(backend):
    async def scenario():
        store = await _store(backend)
        r1 = await store.create(conversation_id=1, lead_id=1, session_id="s", bot_id="bot_a", channel="telegram",
                                 telegram_tester_id="1", chat_id="1", source="faq", outcome="faq_answered", reply_text="x")
        await store.attach_sent_message(r1.id, bot_message_id="1")
        r2 = await store.create(conversation_id=1, lead_id=1, session_id="s", bot_id="bot_b", channel="telegram",
                                 telegram_tester_id="1", chat_id="1", source="llm", outcome="llm_answered", reply_text="y")
        await store.attach_sent_message(r2.id, bot_message_id="2")
        # not eligible: never sent
        await store.create(conversation_id=1, lead_id=1, session_id="s", bot_id="bot_a", channel="telegram",
                            telegram_tester_id="1", chat_id="1", source="llm", outcome="cancelled_by_takeover", reply_text="")

        all_eligible = await store.list_eligible()
        assert {r.id for r in all_eligible} == {r1.id, r2.id}
        only_a = await store.list_eligible(bot_id="bot_a")
        assert [r.id for r in only_a] == [r1.id]
    _run(scenario())


# 8. Regression guard: the ORM schema has no column that could accumulate secrets/PII,
# and outcome/source enums only ever produce feedback-eligible rows via the eligible set.
def test_answer_context_never_stores_secrets_and_outcome_enum_is_closed():
    from app.integrations.crm.db import AnswerContext
    columns = {c.name for c in AnswerContext.__table__.columns}
    forbidden_substrings = ("system_prompt", "secret", "api_key", "raw_output", "webhook_secret")
    for col in columns:
        for forbidden in forbidden_substrings:
            assert forbidden not in col, f"unexpected sensitive column: {col}"
    assert FEEDBACK_ELIGIBLE_OUTCOMES == {
        "faq_answered", "llm_answered", "safe_fallback", "validator_blocked",
        "budget_fallback", "model_error_fallback", "handoff_only",
    }

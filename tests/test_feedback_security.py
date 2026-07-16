"""Increment 7: callback authorization (brief scenarios 19-26, §7). A callback is
NEVER trusted just because it came from Telegram — `FeedbackService.handle_callback`
re-verifies every condition from scratch. On ANY violation: no Feedback row is
created/changed, the callback gets a short neutral ack, and a technical audit event
(`feedback_callback_rejected`) is written with NO extra data (just a short reason
code — never raw callback_data/PII)."""
from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.core import telegram_sessions
from app.core.feedback_service import FeedbackService
from app.integrations.panel.answer_context_store import get_answer_context_store, reset as reset_ctx
from app.integrations.panel.audit_store import get_audit_store
from app.integrations.panel.feedback_store import get_feedback_store, reset as reset_fb


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


async def _sent_context(bot_id: str, tester_id: str, *, source="faq", outcome="faq_answered"):
    session = await telegram_sessions.ensure_active_session(bot_id, tester_id, external_chat_id=tester_id)
    ctx = await get_answer_context_store().create(
        conversation_id=session.conversation.id, lead_id=session.lead.id,
        session_id=session.conversation.session_id, bot_id=bot_id, channel="telegram",
        telegram_tester_id=tester_id, chat_id=tester_id, source=source, outcome=outcome, reply_text="x",
    )
    await get_answer_context_store().attach_sent_message(ctx.id, bot_message_id="1", provider_bot_message_id="1")
    return await get_answer_context_store().get(ctx.id)


async def _assert_rejected(adapter, reason_seen: bool = True):
    assert await get_feedback_store().list_all() == []
    rejected = await get_audit_store().list_by_event_type("feedback_callback_rejected")
    assert len(rejected) >= 1
    # never leaks raw text/PII into the audit record
    assert rejected[-1].reason and "@" not in (rejected[-1].reason or "")
    assert len(adapter.acks) == 1
    assert adapter.acks[0][1]  # a neutral text was sent


# 19. Malformed callback_data.
def test_malformed_callback_data_rejected(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [1901])
    adapter = _RecordingAdapter()
    _run(FeedbackService().handle_callback(
        bot_id="secb1", adapter=adapter, callback_query_id="cb1", tester_id=1901, chat_id=1901, data="not-a-valid-format",
    ))
    _run(_assert_rejected(adapter))


# 20. Non-allowlisted tester.
def test_non_allowlisted_tester_rejected(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [1])  # 2002 not in it
    adapter = _RecordingAdapter()
    _run(FeedbackService().handle_callback(
        bot_id="secb2", adapter=adapter, callback_query_id="cb2", tester_id=2002, chat_id=2002, data="fb:whatever:ok",
    ))
    _run(_assert_rejected(adapter))


# 21. Unknown feedback_token.
def test_unknown_token_rejected(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [2101])
    adapter = _RecordingAdapter()
    _run(FeedbackService().handle_callback(
        bot_id="secb3", adapter=adapter, callback_query_id="cb3", tester_id=2101, chat_id=2101,
        data="fb:does-not-exist:ok",
    ))
    _run(_assert_rejected(adapter))


# 22. Token belongs to a DIFFERENT bot_id.
def test_bot_mismatch_rejected(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [2201])
        ctx = await _sent_context("secb4_owner", "2201")
        adapter = _RecordingAdapter()
        await FeedbackService().handle_callback(
            bot_id="secb4_other", adapter=adapter, callback_query_id="cb4", tester_id=2201, chat_id=2201,
            data=f"fb:{ctx.feedback_token}:ok",
        )
        await _assert_rejected(adapter)
    _run(scenario())


# 23. Token's telegram_tester_id doesn't match the caller (someone else's answer).
def test_tester_mismatch_rejected(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [2301, 2302])
        ctx = await _sent_context("secb5", "2301")
        adapter = _RecordingAdapter()
        await FeedbackService().handle_callback(
            bot_id="secb5", adapter=adapter, callback_query_id="cb5", tester_id=2302, chat_id=2302,
            data=f"fb:{ctx.feedback_token}:ok",
        )
        await _assert_rejected(adapter)
    _run(scenario())


# 24. Answer's source is not an automatic-answer source (defensive — manual/command
# replies never get an answer_context row in the first place, but the guard must hold
# even if one somehow existed).
def test_non_automatic_source_rejected(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [2401])
        ctx = await _sent_context("secb6", "2401", source="manual", outcome="manual_reply")
        adapter = _RecordingAdapter()
        await FeedbackService().handle_callback(
            bot_id="secb6", adapter=adapter, callback_query_id="cb6", tester_id=2401, chat_id=2401,
            data=f"fb:{ctx.feedback_token}:ok",
        )
        await _assert_rejected(adapter)
    _run(scenario())


# 25. Answer was never actually sent (bot_message_id absent).
def test_answer_not_sent_rejected(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [2501])
        session = await telegram_sessions.ensure_active_session("secb7", "2501", external_chat_id="2501")
        ctx = await get_answer_context_store().create(
            conversation_id=session.conversation.id, lead_id=session.lead.id,
            session_id=session.conversation.session_id, bot_id="secb7", channel="telegram",
            telegram_tester_id="2501", chat_id="2501", source="llm", outcome="cancelled_by_takeover", reply_text="",
        )
        adapter = _RecordingAdapter()
        await FeedbackService().handle_callback(
            bot_id="secb7", adapter=adapter, callback_query_id="cb7", tester_id=2501, chat_id=2501,
            data=f"fb:{ctx.feedback_token}:ok",
        )
        await _assert_rejected(adapter)
    _run(scenario())


# 26. Session not owned by the caller (conversation belongs to a different tester).
def test_session_not_owned_rejected(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [2601, 2602])
        ctx = await _sent_context("secb8", "2601")
        # Forge a token whose answer_context.telegram_tester_id we override to look like
        # the caller, but whose linked conversation actually belongs to someone else —
        # exercises the SEPARATE conversation-ownership check (not just the tester_id
        # field equality check in scenario 23).
        from app.integrations.panel.answer_context_store import get_answer_context_store as _s
        raw_store = _s()
        raw_store._rows[ctx.id].telegram_tester_id = "2602"  # type: ignore[attr-defined]
        raw_store._rows[ctx.id].chat_id = "2602"  # type: ignore[attr-defined]
        adapter = _RecordingAdapter()
        await FeedbackService().handle_callback(
            bot_id="secb8", adapter=adapter, callback_query_id="cb8", tester_id=2602, chat_id=2602,
            data=f"fb:{ctx.feedback_token}:ok",
        )
        await _assert_rejected(adapter)
    _run(scenario())

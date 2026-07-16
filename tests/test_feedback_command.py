"""Increment 7: `/feedback <text>` command (brief scenarios 41-44, §10) — finishes the
Increment-4 stub. Attaches the comment to the LAST automatic `answer_context` of the
current session; never calls FAQ/LLM; never changes qualification/lead_status."""
from __future__ import annotations

import asyncio

import pytest

from app.channels.base import Message
from app.config import settings
from app.core import faq_kb, telegram_commands, telegram_sessions
from app.core.feedback_service import NO_AUTOMATIC_ANSWER_TEXT
from app.integrations.panel.answer_context_store import get_answer_context_store, reset as reset_ctx
from app.integrations.panel.feedback_store import get_feedback_store, reset as reset_fb
from app.integrations.panel.leadstore import get_lead_store


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate():
    faq_kb.reset()
    reset_ctx()
    reset_fb()
    yield
    faq_kb.reset()
    reset_ctx()
    reset_fb()


class _RecordingOrch:
    def __init__(self):
        self.bot = None
        self.handled: list[Message] = []

    async def _bots_on(self):
        return True

    async def handle(self, msg):
        self.handled.append(msg)


class _RecordingAdapter:
    channel = "telegram"

    def __init__(self):
        self.sent: list[tuple[str, str, dict | None]] = []

    async def send(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((chat_id, text, reply_markup))
        return None

    async def answer_callback(self, callback_query_id, text="", *, show_alert=False):
        return None


async def _sent_context(bot_id: str, tester_id: str, *, reply_text="x"):
    session = await telegram_sessions.ensure_active_session(bot_id, tester_id, external_chat_id=tester_id)
    ctx = await get_answer_context_store().create(
        conversation_id=session.conversation.id, lead_id=session.lead.id,
        session_id=session.conversation.session_id, bot_id=bot_id, channel="telegram",
        telegram_tester_id=tester_id, chat_id=tester_id, source="llm", outcome="llm_answered", reply_text=reply_text,
    )
    await get_answer_context_store().attach_sent_message(ctx.id, bot_message_id="1", provider_bot_message_id="1")
    return await get_answer_context_store().get(ctx.id), session


# 41. Attaches the comment to the LAST automatic answer_context of the session.
def test_feedback_command_attaches_to_last_automatic_answer():
    async def scenario():
        bot_id, uid = "fbc1", "4101"
        ctx_old, session = await _sent_context(bot_id, uid, reply_text="старый ответ")
        ctx_new, _ = await _sent_context(bot_id, uid, reply_text="новый ответ")

        reply = await telegram_commands.handle_command(
            bot_id=bot_id, external_user_id=uid, external_chat_id=uid, command="/feedback", args="ответ неточный",
        )
        assert reply == "Комментарий сохранён."

        fb_new = await get_feedback_store().get_by_answer_context_and_tester(ctx_new.id, uid)
        fb_old = await get_feedback_store().get_by_answer_context_and_tester(ctx_old.id, uid)
        assert fb_new is not None and fb_new.comment == "ответ неточный"
        assert fb_old is None
    _run(scenario())


# 42. No automatic answer yet -> honest "no answer to attach to" reply.
def test_feedback_command_no_automatic_answer_yet():
    async def scenario():
        bot_id, uid = "fbc2", "4201"
        reply = await telegram_commands.handle_command(
            bot_id=bot_id, external_user_id=uid, external_chat_id=uid, command="/feedback", args="что-то не так",
        )
        assert reply == NO_AUTOMATIC_ANSWER_TEXT
    _run(scenario())


# 43. /feedback never calls FAQ/LLM, never changes qualification/lead_status.
def test_feedback_command_never_touches_faq_llm_or_lead_status():
    async def scenario():
        bot_id, uid = "fbc3", "4301"
        ctx, session = await _sent_context(bot_id, uid)
        before_status = session.lead.lead_status
        before_qual = dict(session.lead.qualification)

        orch = _RecordingOrch()
        adapter = _RecordingAdapter()
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="/feedback сколько стоит на самом деле?", kind="text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=adapter, orchestrator=orch)

        assert orch.handled == []  # never touches FAQ/LLM
        lead = await get_lead_store().get_lead(session.lead.id)
        assert lead.lead_status == before_status
        assert dict(lead.qualification) == before_qual
    _run(scenario())


# 44. Empty /feedback args -> unchanged short instruction reply (Increment-4 behavior
# preserved for the empty-args edge case).
def test_feedback_command_empty_args_unchanged():
    async def scenario():
        bot_id, uid = "fbc4", "4401"
        reply = await telegram_commands.handle_command(
            bot_id=bot_id, external_user_id=uid, external_chat_id=uid, command="/feedback", args="   ",
        )
        assert "напишите комментарий" in reply.lower()
    _run(scenario())

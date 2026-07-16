"""Increment 7: pending-comment state machine (brief scenarios 32-40, §9/§16). Pressing
💬 Комментарий sets a `pending(target answer_context_id, expires_at)` keyed by
`(bot_id, telegram_tester_id, session_id)` — NOT a global flag. The next non-command
text from that tester is captured as the comment and NEVER reaches FAQ/LLM/
qualification, and is NEVER logged to the legacy panel as a client message. `/cancel`
clears it. An expired pending makes the next message an ordinary message again."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.channels.base import Message
from app.config import settings
from app.core import faq_kb, feedback_service, telegram_commands, telegram_sessions
from app.core.feedback_service import FeedbackService
from app.integrations.panel.answer_context_store import get_answer_context_store, reset as reset_ctx
from app.integrations.panel.feedback_store import get_feedback_store, reset as reset_fb
from app.integrations.panel.store import get_conversation_store


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    faq_kb.reset()
    reset_ctx()
    reset_fb()
    feedback_service.reset_pending_comments()
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [])
    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", [])
    yield
    faq_kb.reset()
    reset_ctx()
    reset_fb()
    feedback_service.reset_pending_comments()


class _RecordingAdapter:
    channel = "telegram"

    def __init__(self):
        self.sent: list[tuple[str, str, dict | None]] = []
        self.acks: list[tuple[str, str, bool]] = []

    async def send(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((chat_id, text, reply_markup))
        return "pmid"

    async def answer_callback(self, callback_query_id, text="", *, show_alert=False):
        self.acks.append((callback_query_id, text, show_alert))


class _RecordingOrch:
    def __init__(self):
        self.bot = None
        self.handled: list[Message] = []

    async def _bots_on(self):
        return True

    async def handle(self, msg):
        self.handled.append(msg)


def _msg(uid: str, text: str) -> Message:
    return Message(channel="telegram", user_id=uid, chat_id=uid, text=text, kind="text")


async def _sent_context(bot_id: str, tester_id: str, *, reply_text="x"):
    session = await telegram_sessions.ensure_active_session(bot_id, tester_id, external_chat_id=tester_id)
    ctx = await get_answer_context_store().create(
        conversation_id=session.conversation.id, lead_id=session.lead.id,
        session_id=session.conversation.session_id, bot_id=bot_id, channel="telegram",
        telegram_tester_id=tester_id, chat_id=tester_id, source="faq", outcome="faq_answered", reply_text=reply_text,
    )
    await get_answer_context_store().attach_sent_message(ctx.id, bot_message_id="1", provider_bot_message_id="1")
    return await get_answer_context_store().get(ctx.id), session


# 32. Pressing 💬 Комментарий sets pending, sends a PERSISTENT chat message (comment
# mode stays visible after the popup closes — Fable UX review #1) and acks with a plain
# (non-alert) spinner-clearing text. The persistent message is sent via the adapter and
# is NEVER logged to the panel (verified in test 32b it stays out of the LLM history).
def test_comment_button_sets_pending_sends_persistent_message_and_plain_ack(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [3201])
        ctx, session = await _sent_context("cmt1", "3201")
        adapter = _RecordingAdapter()
        await FeedbackService().handle_callback(
            bot_id="cmt1", adapter=adapter, callback_query_id="cb", tester_id=3201, chat_id=3201,
            data=f"fb:{ctx.feedback_token}:cmt",
        )
        # plain ack (spinner clears), NOT a show_alert popup
        assert adapter.acks[-1][2] is False
        # persistent, visible comment-mode message in the chat
        assert adapter.sent == [("3201", feedback_service.COMMENT_MODE_MESSAGE, None)]
        assert "/cancel" in feedback_service.COMMENT_MODE_MESSAGE
        target = feedback_service.get_pending_comment_target("cmt1", "3201", session.conversation.session_id)
        assert target == ctx.id
    _run(scenario())


# 32b. §16: the comment-mode message must NEVER enter the LLM history. It is sent
# straight through the adapter with NO panel `add_message`, so `_build_history_messages`
# (which reads only panel messages) never sees it.
def test_comment_mode_message_not_in_llm_history(monkeypatch):
    async def scenario():
        from app.core.ai_reply import _build_history_messages
        from app.integrations.panel.store import get_conversation_store

        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [3211])
        ctx, session = await _sent_context("cmt1b", "3211")
        adapter = _RecordingAdapter()
        await FeedbackService().handle_callback(
            bot_id="cmt1b", adapter=adapter, callback_query_id="cb", tester_id=3211, chat_id=3211,
            data=f"fb:{ctx.feedback_token}:cmt",
        )
        # nothing about the comment-mode message reached the panel...
        panel_conv = await get_conversation_store().get("cmt1b:3211")
        texts = [m.text for m in panel_conv.messages] if panel_conv else []
        assert feedback_service.COMMENT_MODE_MESSAGE not in texts
        # ...and therefore it is absent from the LLM history built off the panel.
        history = await _build_history_messages("cmt1b:3211")
        assert all(feedback_service.COMMENT_MODE_MESSAGE != m["content"] for m in history)
    _run(scenario())


# 33. Next text message is saved as the comment, replies "Комментарий сохранён.",
# clears pending.
def test_next_message_saved_as_comment_and_pending_cleared(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [3301])
        ctx, session = await _sent_context("cmt2", "3301")
        feedback_service.set_pending_comment("cmt2", "3301", session.conversation.session_id, ctx.id)

        adapter = _RecordingAdapter()
        orch = _RecordingOrch()
        await telegram_commands.route_message(
            _msg("3301", "тут были неверные сроки"), bot_id="cmt2", adapter=adapter, orchestrator=orch,
        )

        assert adapter.sent == [("3301", "Комментарий сохранён.", None)]
        fb = await get_feedback_store().get_by_answer_context_and_tester(ctx.id, "3301")
        assert fb is not None
        assert fb.comment == "тут были неверные сроки"
        assert feedback_service.get_pending_comment_target("cmt2", "3301", session.conversation.session_id) is None
    _run(scenario())


# 34. That comment text NEVER reaches FAQ/LLM and is NEVER logged as a client message.
def test_comment_text_never_reaches_faq_llm_or_panel(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [3401])
        await faq_kb.get_faq_kb_store().create_draft({
            "canonical_question": "тут были неверные сроки", "answer_ru": "не должно совпасть",
            "answer_ky": None, "category": "general", "priority": 5, "handoff_only": False,
        }, [], "mgr")
        entry = (await faq_kb.get_faq_kb_store().list_entries())[0]
        await faq_kb.get_faq_kb_store().publish(entry.id, "mgr", confirm=True)

        ctx, session = await _sent_context("cmt3", "3401")
        feedback_service.set_pending_comment("cmt3", "3401", session.conversation.session_id, ctx.id)

        adapter = _RecordingAdapter()
        orch = _RecordingOrch()
        await telegram_commands.route_message(
            _msg("3401", "тут были неверные сроки"), bot_id="cmt3", adapter=adapter, orchestrator=orch,
        )

        assert orch.handled == []  # never routed to FAQ/LLM
        assert adapter.sent == [("3401", "Комментарий сохранён.", None)]  # not the FAQ answer
        panel_conv = await get_conversation_store().get(f"cmt3:3401")
        if panel_conv is not None:
            assert not any(m.sender == "client" and m.text == "тут были неверные сроки" for m in panel_conv.messages)
    _run(scenario())


# 35. /cancel clears pending -> the NEXT message is treated normally again.
def test_cancel_clears_pending_and_next_message_is_normal(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [3501])
        ctx, session = await _sent_context("cmt4", "3501")
        feedback_service.set_pending_comment("cmt4", "3501", session.conversation.session_id, ctx.id)

        adapter = _RecordingAdapter()
        orch = _RecordingOrch()
        await telegram_commands.route_message(_msg("3501", "/cancel"), bot_id="cmt4", adapter=adapter, orchestrator=orch)
        assert adapter.sent == [("3501", feedback_service.CANCEL_CLEARED_TEXT, None)]
        assert feedback_service.get_pending_comment_target("cmt4", "3501", session.conversation.session_id) is None

        # next message is NOT swallowed as a comment.
        adapter2 = _RecordingAdapter()
        await telegram_commands.route_message(_msg("3501", "просто вопрос"), bot_id="cmt4", adapter=adapter2, orchestrator=orch)
        assert adapter2.sent[0][1] != "Комментарий сохранён."
    _run(scenario())


# 36. /cancel with nothing pending -> honest "Нечего отменять."
def test_cancel_without_pending_says_nothing_to_cancel(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [3601])
        adapter = _RecordingAdapter()
        await telegram_commands.route_message(
            _msg("3601", "/cancel"), bot_id="cmt5", adapter=adapter, orchestrator=_RecordingOrch(),
        )
        assert adapter.sent == [("3601", feedback_service.CANCEL_NOTHING_TEXT, None)]
    _run(scenario())


# 37. Expired pending -> the message is a normal client message again (NOT captured).
def test_expired_pending_falls_through_to_normal_message(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [3701])
        ctx, session = await _sent_context("cmt6", "3701")
        key = feedback_service._pending_key("cmt6", "3701", session.conversation.session_id)
        feedback_service._pending_comments[key] = (ctx.id, datetime.now(timezone.utc) - timedelta(seconds=1))

        adapter = _RecordingAdapter()
        orch = _RecordingOrch()
        await telegram_commands.route_message(
            _msg("3701", "новый обычный вопрос"), bot_id="cmt6", adapter=adapter, orchestrator=orch,
        )
        # expired -> not swallowed as a comment reply
        assert adapter.sent == [] or adapter.sent[0][1] != "Комментарий сохранён."
        fb = await get_feedback_store().get_by_answer_context_and_tester(ctx.id, "3701")
        assert fb is None  # nothing was attached
    _run(scenario())


# 38. Pending is scoped per (bot_id, tester_id, session_id) — another tester's message
# is never mistaken for the first tester's pending comment.
def test_pending_scoped_per_bot_tester_session(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [3801, 3802])
        ctx, session = await _sent_context("cmt7", "3801")
        feedback_service.set_pending_comment("cmt7", "3801", session.conversation.session_id, ctx.id)

        # a DIFFERENT tester on the same bot sends text — must not be swallowed.
        adapter = _RecordingAdapter()
        orch = _RecordingOrch()
        await telegram_commands.route_message(
            _msg("3802", "привет, я другой тестировщик"), bot_id="cmt7", adapter=adapter, orchestrator=orch,
        )
        assert adapter.sent == [] or adapter.sent[0][1] != "Комментарий сохранён."
        fb_other = await get_feedback_store().get_by_answer_context_and_tester(ctx.id, "3802")
        assert fb_other is None
        # the original tester's pending is still intact.
        assert feedback_service.get_pending_comment_target("cmt7", "3801", session.conversation.session_id) == ctx.id
    _run(scenario())


# 39. Pressing 💬 again re-targets pending to the NEW answer (last press wins).
def test_pressing_comment_again_retargets_pending(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [3901])
        ctx1, session = await _sent_context("cmt8", "3901", reply_text="first answer")
        ctx2, _ = await _sent_context("cmt8", "3901", reply_text="second answer")
        adapter = _RecordingAdapter()
        svc = FeedbackService()
        await svc.handle_callback(bot_id="cmt8", adapter=adapter, callback_query_id="cb1", tester_id=3901,
                                   chat_id=3901, data=f"fb:{ctx1.feedback_token}:cmt")
        await svc.handle_callback(bot_id="cmt8", adapter=adapter, callback_query_id="cb2", tester_id=3901,
                                   chat_id=3901, data=f"fb:{ctx2.feedback_token}:cmt")
        target = feedback_service.get_pending_comment_target("cmt8", "3901", session.conversation.session_id)
        assert target == ctx2.id  # last press wins, not the first
    _run(scenario())


# 40. Pressing 💬 on an EARLIER answer (not the latest one in the conversation) still
# attaches the eventual comment to THAT earlier answer, not to whatever is newest.
def test_comment_targets_the_pressed_answer_not_the_latest(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [4001])
        ctx_old, session = await _sent_context("cmt9", "4001", reply_text="старый ответ")
        ctx_new, _ = await _sent_context("cmt9", "4001", reply_text="новый ответ")
        assert ctx_old.id != ctx_new.id

        feedback_service.set_pending_comment("cmt9", "4001", session.conversation.session_id, ctx_old.id)

        adapter = _RecordingAdapter()
        orch = _RecordingOrch()
        await telegram_commands.route_message(
            _msg("4001", "это про старый ответ"), bot_id="cmt9", adapter=adapter, orchestrator=orch,
        )
        fb_old = await get_feedback_store().get_by_answer_context_and_tester(ctx_old.id, "4001")
        fb_new = await get_feedback_store().get_by_answer_context_and_tester(ctx_new.id, "4001")
        assert fb_old is not None and fb_old.comment == "это про старый ответ"
        assert fb_new is None
    _run(scenario())

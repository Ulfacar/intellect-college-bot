"""Increment 7: inline feedback buttons (brief scenarios 9-18) — keyboard layout/codes,
the enabled+allowlisted display gate, and that every automatic-answer send point
(FAQ/handoff/LLM) attaches `reply_markup` built from that answer's `feedback_token`
while non-automatic responses (commands/status) and the non-text fallback never do."""
from __future__ import annotations

import asyncio

import pytest

from app.agent.structured_llm import StructuredCallResult, UsageInfo
from app.channels.base import Message
from app.config import settings
from app.core import ai_reply, faq_kb, flags, telegram_commands, telegram_sessions
from app.core.feedback_service import build_feedback_keyboard
from app.integrations.panel.ai_log_store import reset as reset_ai_log
from app.integrations.panel.answer_context_store import get_answer_context_store, reset as reset_ctx


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    faq_kb.reset()
    flags.reset()
    reset_ai_log()
    reset_ctx()
    monkeypatch.setattr(settings, "telegram_feedback_enabled", True)
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [])
    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", [])
    yield
    faq_kb.reset()
    flags.reset()
    reset_ai_log()
    reset_ctx()


class _RecordingAdapter:
    channel = "telegram"

    def __init__(self, *, fail: bool = False):
        self.sent: list[tuple[str, str, dict | None]] = []
        self._fail = fail

    async def send(self, chat_id, text, reply_markup=None, **kw):
        if self._fail:
            raise RuntimeError("boom")
        self.sent.append((chat_id, text, reply_markup))
        return "provider-msg-1"

    async def answer_callback(self, callback_query_id, text="", *, show_alert=False):
        return None


class _RecordingOrch:
    def __init__(self, bot=None):
        self.bot = bot

    async def _bots_on(self):
        return True


async def _publish_faq_entry(*, question="сколько стоит", answer="6500$/год", handoff_only=False):
    store = faq_kb.get_faq_kb_store()
    entry = await store.create_draft({
        "canonical_question": question, "answer_ru": answer, "answer_ky": None,
        "category": "tuition", "priority": 10, "handoff_only": handoff_only,
    }, [], "mgr")
    result = await store.publish(entry.id, "mgr", confirm=True)
    assert result.ok
    return entry


def _msg(uid: str, text: str, kind: str = "text") -> Message:
    return Message(channel="telegram", user_id=uid, chat_id=uid, text=text, kind=kind)


# --------------------------------------------------------------------------------------
# 9. Keyboard layout: 3 rows, exact button texts/codes.
# --------------------------------------------------------------------------------------

def test_keyboard_layout_matches_spec():
    # Phone-friendly re-row (Fable UX review #3): 3/2/2, no button dropped.
    kb = build_feedback_keyboard("tok123456789")
    rows = kb["inline_keyboard"]
    assert len(rows) == 3
    assert [b["text"] for b in rows[0]] == ["✅ Правильно", "⚠️ Неточно", "❌ Неправильно"]
    assert [b["text"] for b in rows[1]] == ["🔥 Нужно дожать", "🧊 Не дожимать"]
    assert [b["text"] for b in rows[2]] == ["👤 Нужен был менеджер", "💬 Комментарий"]


# --------------------------------------------------------------------------------------
# 10. callback_data format `fb:<token>:<code>`, all under 64 bytes.
# --------------------------------------------------------------------------------------

def test_callback_data_format_and_byte_length():
    from app.integrations.panel.answer_context_store import new_feedback_token
    token = new_feedback_token()
    kb = build_feedback_keyboard(token)
    codes = []
    for row in kb["inline_keyboard"]:
        for btn in row:
            data = btn["callback_data"]
            assert data.startswith(f"fb:{token}:")
            assert len(data.encode("utf-8")) < 64
            codes.append(data.rsplit(":", 1)[1])
    assert codes == ["ok", "inacc", "bad", "push", "nopush", "mgr", "cmt"]
    # never leak text/phone/history/secrets/JSON into callback_data
    for row in kb["inline_keyboard"]:
        for btn in row:
            assert "{" not in btn["callback_data"]
            assert "@" not in btn["callback_data"]


# --------------------------------------------------------------------------------------
# 11. FAQ match (enabled + allowlisted) -> reply_markup attached from THIS answer's token.
# --------------------------------------------------------------------------------------

def test_faq_answer_attaches_keyboard_for_its_own_token(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [111])
        await _publish_faq_entry()
        bot_id, uid = "btn9_1", "111"
        adapter = _RecordingAdapter()
        await telegram_commands.route_message(
            _msg(uid, "сколько стоит"), bot_id=bot_id, adapter=adapter, orchestrator=_RecordingOrch(),
        )
        assert len(adapter.sent) == 1
        chat_id, text, markup = adapter.sent[0]
        assert text == "6500$/год"
        assert markup is not None

        session = await telegram_sessions.get_active_session(bot_id, uid)
        ctxs = await get_answer_context_store().list_for_conversation(session[0].id)
        assert len(ctxs) == 1
        ctx = ctxs[0]
        assert ctx.source == "faq" and ctx.outcome == "faq_answered"
        assert ctx.bot_message_id is not None
        assert markup == {
            "inline_keyboard": [
                [{"text": "✅ Правильно", "callback_data": f"fb:{ctx.feedback_token}:ok"},
                 {"text": "⚠️ Неточно", "callback_data": f"fb:{ctx.feedback_token}:inacc"},
                 {"text": "❌ Неправильно", "callback_data": f"fb:{ctx.feedback_token}:bad"}],
                [{"text": "🔥 Нужно дожать", "callback_data": f"fb:{ctx.feedback_token}:push"},
                 {"text": "🧊 Не дожимать", "callback_data": f"fb:{ctx.feedback_token}:nopush"}],
                [{"text": "👤 Нужен был менеджер", "callback_data": f"fb:{ctx.feedback_token}:mgr"},
                 {"text": "💬 Комментарий", "callback_data": f"fb:{ctx.feedback_token}:cmt"}],
            ]
        }
    _run(scenario())


# --------------------------------------------------------------------------------------
# 12. FAQ handoff_only match -> keyboard attached too, source="handoff".
# --------------------------------------------------------------------------------------

def test_faq_handoff_only_attaches_keyboard():
    async def scenario():
        settings.telegram_allowed_user_ids = [222]
        await _publish_faq_entry(question="какой проходной балл", answer="90+ (внутр.)", handoff_only=True)
        bot_id, uid = "btn9_2", "222"
        adapter = _RecordingAdapter()
        await telegram_commands.route_message(
            _msg(uid, "какой проходной балл"), bot_id=bot_id, adapter=adapter, orchestrator=_RecordingOrch(),
        )
        _chat, text, markup = adapter.sent[0]
        assert text == telegram_commands.FAQ_HANDOFF_SAFE_PHRASE
        assert markup is not None
        session = await telegram_sessions.get_active_session(bot_id, uid)
        ctxs = await get_answer_context_store().list_for_conversation(session[0].id)
        assert ctxs[0].source == "handoff" and ctxs[0].outcome == "handoff_only"
        settings.telegram_allowed_user_ids = []
    _run(scenario())


# --------------------------------------------------------------------------------------
# 13. Feedback GLOBALLY disabled -> answer_context row still written, but NO keyboard.
# --------------------------------------------------------------------------------------

def test_feedback_disabled_writes_context_but_no_keyboard(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_feedback_enabled", False)
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [333])
        await _publish_faq_entry()
        bot_id, uid = "btn9_3", "333"
        adapter = _RecordingAdapter()
        await telegram_commands.route_message(
            _msg(uid, "сколько стоит"), bot_id=bot_id, adapter=adapter, orchestrator=_RecordingOrch(),
        )
        _chat, _text, markup = adapter.sent[0]
        assert markup is None
        session = await telegram_sessions.get_active_session(bot_id, uid)
        ctxs = await get_answer_context_store().list_for_conversation(session[0].id)
        assert len(ctxs) == 1  # still written — write is unconditional, display is not
    _run(scenario())


# --------------------------------------------------------------------------------------
# 14. Tester not allowlisted -> answer_context written, NO keyboard (defense in depth —
# prepare_answer independently re-checks allowlist, even though route_message is only
# reachable for allowlisted testers via the real webhook gate in app/main.py).
# --------------------------------------------------------------------------------------

def test_non_allowlisted_tester_gets_no_keyboard(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [1])  # 444 not in it
        await _publish_faq_entry()
        bot_id, uid = "btn9_4", "444"
        adapter = _RecordingAdapter()
        await telegram_commands.route_message(
            _msg(uid, "сколько стоит"), bot_id=bot_id, adapter=adapter, orchestrator=_RecordingOrch(),
        )
        _chat, _text, markup = adapter.sent[0]
        assert markup is None
    _run(scenario())


# --------------------------------------------------------------------------------------
# 15. Command responses (/status, /help, ...) never carry buttons — no answer_context
# row is created for them at all (they never go through prepare_answer).
# --------------------------------------------------------------------------------------

def test_command_responses_never_get_buttons(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [555])
        bot_id, uid = "btn9_5", "555"
        adapter = _RecordingAdapter()
        for cmd in ("/status", "/help"):
            await telegram_commands.route_message(
                _msg(uid, cmd), bot_id=bot_id, adapter=adapter, orchestrator=_RecordingOrch(),
            )
        for _chat, _text, markup in adapter.sent:
            assert markup is None
        assert (await get_answer_context_store().list_for_conversation(0)) == []
    _run(scenario())


# --------------------------------------------------------------------------------------
# 16. Manager/paused-owner dialogs (manual manager reply territory) never create an
# answer_context row — route_message stops at `_log_to_legacy_panel` for them.
# --------------------------------------------------------------------------------------

def test_manager_owned_dialog_creates_no_answer_context():
    async def scenario():
        from app.core.conversation_service import ConversationService

        bot_id, uid = "btn9_6", "666"
        session = await telegram_sessions.ensure_active_session(bot_id, uid)
        await ConversationService().request_manager(session.conversation.id)

        adapter = _RecordingAdapter()
        await telegram_commands.route_message(
            _msg(uid, "какие направления?"), bot_id=bot_id, adapter=adapter, orchestrator=_RecordingOrch(),
        )
        assert adapter.sent == []  # bot stays silent
        ctxs = await get_answer_context_store().list_for_conversation(session.conversation.id)
        assert ctxs == []
    _run(scenario())


# --------------------------------------------------------------------------------------
# 17. LLM successful send -> keyboard attached (app/core/ai_reply.py send-wrapping).
# --------------------------------------------------------------------------------------

def _arguments(reply="Здравствуйте! После какого класса поступаете?"):
    return {
        "reply": reply, "language": "ru",
        "answer_basis": {"knowledge_entry_ids": [], "facts_used": []},
        "classification": {
            "intent": "asks_general_info", "confidence": 0.95, "evidence": "клиент спросил",
            "lead_temperature": "warm", "suggested_status": "in_progress", "next_action_type": None,
            "next_action_at": None, "should_handoff": False, "handoff_reason": None,
            "qualification_updates": {},
        },
        "summary_update": "Клиент интересуется поступлением.",
        "safety": {"uncertain": False, "unsupported_claims": [], "requires_human_confirmation": False},
    }


def test_llm_sent_reply_attaches_keyboard(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [777])
        monkeypatch.setattr(settings, "llm_model_main", "anthropic/claude-haiku-4.5")

        async def _caller(*, system, messages, model, max_output_tokens, timeout_seconds):
            return StructuredCallResult(
                ok=True, arguments=_arguments(), model=model, latency_ms=10.0, retry_count=0,
                generation_id="gen", usage=UsageInfo(input_tokens=10, output_tokens=5, total_tokens=15, cost=0.001, cost_source="provider"),
            )
        monkeypatch.setattr(ai_reply, "_llm_caller", _caller)

        bot_id, uid = "btn9_7", "777"
        session = await telegram_sessions.ensure_active_session(bot_id, uid)
        adapter = _RecordingAdapter()

        outcome = await ai_reply.generate_and_send_reply(
            _msg(uid, "хочу узнать о поступлении"), bot_id=bot_id, adapter=adapter,
            orchestrator=_RecordingOrch(), session=session,
        )
        assert outcome == "sent"
        _chat, text, markup = adapter.sent[0]
        assert markup is not None
        ctxs = await get_answer_context_store().list_for_conversation(session.conversation.id)
        assert len(ctxs) == 1
        assert ctxs[0].source == "llm" and ctxs[0].outcome == "llm_answered"
        assert ctxs[0].reply_text == text
    _run(scenario())


# --------------------------------------------------------------------------------------
# 18. Non-text fallback ("голосовые не распознаём") is explicitly OUT of scope for
# Increment 7's answer_context write (not FAQ/LLM/budget/model-error — a fixed honest
# fallback for an unsupported media type) — no row, no buttons.
# --------------------------------------------------------------------------------------

def test_non_text_fallback_creates_no_answer_context():
    async def scenario():
        settings.telegram_allowed_user_ids = [888]
        bot_id, uid = "btn9_8", "888"
        session = await telegram_sessions.ensure_active_session(bot_id, uid)
        adapter = _RecordingAdapter()
        await telegram_commands.route_message(
            _msg(uid, "", kind="non_text"), bot_id=bot_id, adapter=adapter, orchestrator=_RecordingOrch(),
        )
        assert adapter.sent == [(uid, telegram_commands.NON_TEXT_SAFE_FALLBACK, None)]
        ctxs = await get_answer_context_store().list_for_conversation(session.conversation.id)
        assert ctxs == []
        settings.telegram_allowed_user_ids = []
    _run(scenario())

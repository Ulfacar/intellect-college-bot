"""Increment 6: pipeline-integration gating at app/core/telegram_commands.py::route_message
(brief §20 scenarios 1-9) — confirms `app/core/ai_reply.py::generate_and_send_reply` is
called ONLY when every prior gate has passed, and never otherwise. Complements the
existing Increment 4/5 gating tests (tests/test_telegram_commands.py,
tests/test_faq_kb_gates.py) which already cover dialog_owner/on-off/FAQ-hit at the
`orchestrator.handle` boundary — this file re-proves the same boundaries now that
`ai_reply` (not `orchestrator.handle`) is the thing being gated.
"""
from __future__ import annotations

import asyncio

import pytest

from app.channels.base import Message
from app.config import BotConfig, settings
from app.core import ai_reply, faq_kb, flags, telegram_commands, telegram_sessions
from app.core.conversation_service import ConversationService
from app.core.orchestrator import Orchestrator
from app.integrations.panel.ai_log_store import reset as reset_ai_log


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate():
    faq_kb.reset()
    flags.reset()
    reset_ai_log()
    yield
    faq_kb.reset()
    flags.reset()
    reset_ai_log()


class _RecordingOrch:
    def __init__(self, bot=None):
        self.bot = bot

    async def _bots_on(self):
        return True


class _SendOnlyAdapter:
    channel = "telegram"

    def __init__(self):
        self.sent: list[str] = []

    async def send(self, chat_id, text, **kw):
        self.sent.append(text)
        return None


def _patch_recording_ai_reply(monkeypatch):
    calls: list[Message] = []

    async def _fake(msg, *, bot_id, adapter, orchestrator, session):
        calls.append(msg)
        return "sent"
    monkeypatch.setattr(telegram_commands.ai_reply, "generate_and_send_reply", _fake)
    return calls


async def _publish_faq(question="сколько стоит обучение", answer="6500 долларов в год"):
    store = faq_kb.get_faq_kb_store()
    entry = await store.create_draft(
        {"canonical_question": question, "answer_ru": answer, "category": "tuition",
         "priority": 0, "handoff_only": False},
        [{"text": question}], "mgr",
    )
    await store.publish(entry.id, "mgr", confirm=True)
    return entry


# 1. FAQ hit -> ai_reply NEVER called.
def test_faq_hit_never_calls_ai_reply(monkeypatch):
    calls = _patch_recording_ai_reply(monkeypatch)

    async def scenario():
        await _publish_faq()
        bot_id, uid = "g1", "gu1"
        orch = _RecordingOrch()
        adapter = _SendOnlyAdapter()
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="сколько стоит обучение", kind="text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=adapter, orchestrator=orch)
        assert calls == []
        assert adapter.sent == ["6500 долларов в год"]
    _run(scenario())


# 2. Global OFF -> ai_reply NEVER called.
def test_global_off_never_calls_ai_reply(monkeypatch):
    calls = _patch_recording_ai_reply(monkeypatch)

    async def scenario():
        await flags.set_flag("bots_enabled", False)
        bot_id, uid = "g2", "gu2"
        orch = Orchestrator(channel=_SendOnlyAdapter(), bot=BotConfig(id=bot_id, scenario="admission"))
        adapter = _SendOnlyAdapter()
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="сколько стоит", kind="text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=adapter, orchestrator=orch)
        assert calls == []
        assert adapter.sent == []
    _run(scenario())


# 3. Individual OFF (global ON) -> ai_reply NEVER called for that bot.
def test_individual_off_never_calls_ai_reply(monkeypatch):
    calls = _patch_recording_ai_reply(monkeypatch)

    async def scenario():
        bot_id, uid = "g3", "gu3"
        await flags.set_flag("bots_enabled", True)
        await flags.set_flag(f"bots_enabled:{bot_id}", False)
        orch = Orchestrator(channel=_SendOnlyAdapter(), bot=BotConfig(id=bot_id, scenario="admission"))
        adapter = _SendOnlyAdapter()
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="привет", kind="text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=adapter, orchestrator=orch)
        assert calls == []
        assert adapter.sent == []
    _run(scenario())


# 4. dialog_owner=manager -> ai_reply NEVER called.
def test_manager_owner_never_calls_ai_reply(monkeypatch):
    calls = _patch_recording_ai_reply(monkeypatch)

    async def scenario():
        bot_id, uid = "g4", "gu4"
        session = await telegram_sessions.ensure_active_session(bot_id, uid)
        await ConversationService().takeover(session.conversation.id, "manager_x")
        orch = _RecordingOrch()
        adapter = _SendOnlyAdapter()
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="привет", kind="text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=adapter, orchestrator=orch)
        assert calls == []
        assert adapter.sent == []
    _run(scenario())


# 4b. dialog_owner=paused -> ai_reply NEVER called.
def test_paused_owner_never_calls_ai_reply(monkeypatch):
    calls = _patch_recording_ai_reply(monkeypatch)

    async def scenario():
        bot_id, uid = "g4b", "gu4b"
        session = await telegram_sessions.ensure_active_session(bot_id, uid)
        await ConversationService().pause(session.conversation.id)
        orch = _RecordingOrch()
        adapter = _SendOnlyAdapter()
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="привет", kind="text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=adapter, orchestrator=orch)
        assert calls == []
        assert adapter.sent == []
    _run(scenario())


# 5. Command text -> ai_reply NEVER called.
def test_command_never_calls_ai_reply(monkeypatch):
    calls = _patch_recording_ai_reply(monkeypatch)

    async def scenario():
        bot_id, uid = "g5", "gu5"
        orch = _RecordingOrch()
        adapter = _SendOnlyAdapter()
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="/status", kind="text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=adapter, orchestrator=orch)
        assert calls == []
        assert len(adapter.sent) == 1
    _run(scenario())


# 6. Non-allowlisted users never reach route_message at all (blocked earlier in
# app/main.py) — regression-covered by tests/test_telegram_pilot_security.py; here we
# document the invariant at the route_message boundary itself: an empty/service update
# (no user_id) is a no-op, same as before Increment 6.
def test_empty_update_never_calls_ai_reply(monkeypatch):
    calls = _patch_recording_ai_reply(monkeypatch)

    async def scenario():
        adapter = _SendOnlyAdapter()
        msg = Message(channel="telegram", user_id="", chat_id="", text="", kind="text")
        await telegram_commands.route_message(msg, bot_id="g6", adapter=adapter, orchestrator=_RecordingOrch())
        assert calls == []
        assert adapter.sent == []
    _run(scenario())


# 7. Non-text message -> safe fallback, ai_reply NEVER called (no LLM).
def test_non_text_never_calls_ai_reply(monkeypatch):
    calls = _patch_recording_ai_reply(monkeypatch)

    async def scenario():
        bot_id, uid = "g7", "gu7"
        orch = _RecordingOrch()
        adapter = _SendOnlyAdapter()
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="", kind="non_text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=adapter, orchestrator=orch)
        assert calls == []
        assert adapter.sent == [telegram_commands.NON_TEXT_SAFE_FALLBACK]
    _run(scenario())


# 8. Budget exhausted -> ai_reply NEVER called; FAQ still works for OTHER messages.
def test_budget_exhausted_never_calls_ai_reply_but_faq_still_works(monkeypatch):
    calls = _patch_recording_ai_reply(monkeypatch)

    async def scenario():
        await _publish_faq()
        from app.integrations.panel.ai_log_store import get_ai_log_store
        monkeypatch.setattr(settings, "llm_daily_budget_usd", 1.0)
        row = await get_ai_log_store().reserve(
            request_id="pre", conversation_id=None, lead_id=None, bot_id="g8", model="m", prompt_version="v",
        )
        await get_ai_log_store().finalize(row.id, outcome="sent", cost=5.0)

        bot_id, uid = "g8", "gu8"
        orch = _RecordingOrch()
        adapter = _SendOnlyAdapter()

        # non-FAQ question -> would normally hit ai_reply, but budget is exhausted.
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="какие направления есть", kind="text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=adapter, orchestrator=orch)
        assert calls == []
        assert adapter.sent == [ai_reply.BUDGET_EXHAUSTED_FALLBACK]

        # FAQ question still answered without touching ai_reply/LLM at all.
        adapter2 = _SendOnlyAdapter()
        msg2 = Message(channel="telegram", user_id=uid, chat_id=uid, text="сколько стоит обучение", kind="text")
        await telegram_commands.route_message(msg2, bot_id=bot_id, adapter=adapter2, orchestrator=orch)
        assert calls == []
        assert adapter2.sent == ["6500 долларов в год"]
    _run(scenario())


# 9. No FAQ match, budget available, dialog_owner=bot, effective ON -> ai_reply IS
# called exactly once (the "happy path" gate outcome, proving the gates above are not
# accidentally always-block).
def test_normal_message_reaches_ai_reply_exactly_once(monkeypatch):
    calls = _patch_recording_ai_reply(monkeypatch)

    async def scenario():
        bot_id, uid = "g9", "gu9"
        orch = _RecordingOrch()
        adapter = _SendOnlyAdapter()
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="есть ли общежитие", kind="text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=adapter, orchestrator=orch)
        assert len(calls) == 1
        assert calls[0].text == "есть ли общежитие"
    _run(scenario())

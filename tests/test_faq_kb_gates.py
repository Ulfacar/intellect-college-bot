"""Increment 5: pipeline gate ordering (scenarios 31-37 of the brief's §20) —
`dialog_owner` -> effective bot on/off switch -> managed FAQ -> orchestrator. Uses the
same fakes/conventions as tests/test_telegram_commands.py."""
from __future__ import annotations

import asyncio

import pytest

from app.channels.base import Message
from app.config import BotConfig
from app.core import faq_kb, flags, telegram_commands, telegram_sessions
from app.core.conversation_service import ConversationService
from app.core.orchestrator import Orchestrator


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate_faq_kb():
    faq_kb.reset()
    flags.reset()
    yield
    faq_kb.reset()
    flags.reset()


class _RecordingOrch:
    def __init__(self, bot=None):
        self.handled = []
        self.bot = bot

    async def _bots_on(self):
        return True

    async def handle(self, msg):
        self.handled.append(msg)


class _SendOnlyAdapter:
    channel = "telegram"

    def __init__(self):
        self.sent: list[str] = []

    async def send(self, chat_id, text, **kw):
        self.sent.append(text)
        return None


async def _publish_faq(question="сколько стоит обучение", answer="6500 долларов в год", category="general"):
    store = faq_kb.get_faq_kb_store()
    entry = await store.create_draft({
        "canonical_question": question, "answer_ru": answer, "category": category,
        "priority": 5, "handoff_only": False,
    }, [], "mgr")
    result = await store.publish(entry.id, "mgr", confirm=True)
    assert result.ok
    return entry


# --------------------------------------------------------------------------------------
# 31. dialog_owner=manager blocks FAQ entirely (no reply, orchestrator never called).
# --------------------------------------------------------------------------------------

def test_manager_owner_blocks_faq():
    async def scenario():
        await _publish_faq()
        bot_id, uid = "gate_1", "g31"
        session = await telegram_sessions.ensure_active_session(bot_id, uid)
        await ConversationService().request_manager(session.conversation.id)

        orch = _RecordingOrch()
        adapter = _SendOnlyAdapter()
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="сколько стоит обучение", kind="text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=adapter, orchestrator=orch)

        assert adapter.sent == []
        assert orch.handled == []
    _run(scenario())


# --------------------------------------------------------------------------------------
# 32. dialog_owner=paused blocks FAQ entirely.
# --------------------------------------------------------------------------------------

def test_paused_owner_blocks_faq():
    async def scenario():
        await _publish_faq()
        bot_id, uid = "gate_2", "g32"
        session = await telegram_sessions.ensure_active_session(bot_id, uid)
        await ConversationService().pause(session.conversation.id)

        orch = _RecordingOrch()
        adapter = _SendOnlyAdapter()
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="сколько стоит обучение", kind="text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=adapter, orchestrator=orch)

        assert adapter.sent == []
        assert orch.handled == []
    _run(scenario())


# --------------------------------------------------------------------------------------
# 33. Global OFF blocks the managed FAQ auto-reply too (not just the LLM path).
# --------------------------------------------------------------------------------------

def test_global_off_blocks_faq_auto_reply():
    async def scenario():
        await _publish_faq()
        await flags.set_flag("bots_enabled", False)
        bot_id, uid = "gate_3", "g33"

        bot_cfg = BotConfig(id=bot_id, scenario="admission")
        orch = Orchestrator(channel=_SendOnlyAdapter(), bot=bot_cfg)
        adapter = _SendOnlyAdapter()
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="сколько стоит обучение", kind="text")

        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=adapter, orchestrator=orch)

        assert adapter.sent == []  # FAQ auto-reply NOT sent while globally OFF
        # incoming still logged for the manager (parity with the dialog_owner gates)
        from app.integrations.panel.store import get_conversation_store
        panel_conv = await get_conversation_store().get(f"{bot_id}:{uid}")
        assert panel_conv is not None
        assert any(m.text == "сколько стоит обучение" for m in panel_conv.messages)
    _run(scenario())


# --------------------------------------------------------------------------------------
# 34. Individual (per-bot) OFF blocks FAQ for that bot only — another bot still works.
# --------------------------------------------------------------------------------------

def test_individual_off_blocks_faq_for_that_bot_only():
    async def scenario():
        await _publish_faq()
        bot_off, bot_on, uid = "gate_off", "gate_on", "g34"
        await flags.set_flag(f"bots_enabled:{bot_off}", False)

        orch_off = Orchestrator(channel=_SendOnlyAdapter(), bot=BotConfig(id=bot_off, scenario="admission"))
        adapter_off = _SendOnlyAdapter()
        msg_off = Message(channel="telegram", user_id=uid, chat_id=uid, text="сколько стоит обучение", kind="text")
        await telegram_commands.route_message(msg_off, bot_id=bot_off, adapter=adapter_off, orchestrator=orch_off)
        assert adapter_off.sent == []

        orch_on = Orchestrator(channel=_SendOnlyAdapter(), bot=BotConfig(id=bot_on, scenario="admission"))
        adapter_on = _SendOnlyAdapter()
        msg_on = Message(channel="telegram", user_id=uid, chat_id=uid, text="сколько стоит обучение", kind="text")
        await telegram_commands.route_message(msg_on, bot_id=bot_on, adapter=adapter_on, orchestrator=orch_on)
        assert adapter_on.sent == ["6500 долларов в год"]
    _run(scenario())


# --------------------------------------------------------------------------------------
# 35. No FAQ match -> falls through unchanged to orchestrator.handle.
# --------------------------------------------------------------------------------------

def test_no_faq_match_falls_through_to_orchestrator():
    async def scenario():
        await _publish_faq(question="сколько стоит обучение")
        bot_id, uid = "gate_5", "g35"
        orch = _RecordingOrch()
        adapter = _SendOnlyAdapter()
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="есть ли общежитие", kind="text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=adapter, orchestrator=orch)

        assert adapter.sent == []          # FAQ layer sent nothing
        assert len(orch.handled) == 1      # orchestrator DID get called
        assert orch.handled[0].text == "есть ли общежитие"
    _run(scenario())


# --------------------------------------------------------------------------------------
# 36. FAQ match -> orchestrator.handle is NEVER called; no second session created.
# --------------------------------------------------------------------------------------

def test_faq_match_never_calls_orchestrator_and_reuses_session():
    async def scenario():
        await _publish_faq()
        bot_id, uid = "gate_6", "g36"
        orch = _RecordingOrch()
        adapter = _SendOnlyAdapter()
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="сколько стоит обучение", kind="text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=adapter, orchestrator=orch)

        assert orch.handled == []
        assert adapter.sent == ["6500 долларов в год"]

        # Exactly one active session/lead exists — FAQ path did not spawn a second one.
        conv, _lead = await telegram_sessions.get_active_session(bot_id, uid)
        assert conv is not None
        second_msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="сколько стоит обучение", kind="text")
        await telegram_commands.route_message(second_msg, bot_id=bot_id, adapter=adapter, orchestrator=orch)
        conv2, _lead2 = await telegram_sessions.get_active_session(bot_id, uid)
        assert conv2.id == conv.id
    _run(scenario())


# --------------------------------------------------------------------------------------
# 37. Command text never reaches the FAQ matcher, even if it would incidentally match a
#     stored canonical_question.
# --------------------------------------------------------------------------------------

def test_command_text_never_reaches_faq_matcher():
    async def scenario():
        # Publish an entry whose canonical_question happens to look like a command.
        store = faq_kb.get_faq_kb_store()
        entry = await store.create_draft({
            "canonical_question": "/status", "answer_ru": "не должно попасть в ответ FAQ",
            "category": "general", "priority": 0, "handoff_only": False,
        }, [], "mgr")
        await store.publish(entry.id, "mgr", confirm=True)

        bot_id, uid = "gate_7", "g37"
        orch = _RecordingOrch()
        adapter = _SendOnlyAdapter()
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="/status", kind="text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=adapter, orchestrator=orch)

        assert orch.handled == []
        assert len(adapter.sent) == 1
        assert "не должно попасть" not in adapter.sent[0]
        assert "lead_status" in adapter.sent[0]  # real /status command output
    _run(scenario())

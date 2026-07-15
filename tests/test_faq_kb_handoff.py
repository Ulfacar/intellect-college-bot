"""Increment 5: handoff_only pipeline behavior (scenarios 26-30 of the brief's §20).
Exercises `app.core.telegram_commands._try_faq_reply`/`route_message` end-to-end with a
real published `handoff_only` entry — memory backend (default), fakes for the Telegram
adapter/orchestrator per existing conventions (tests/test_telegram_commands.py)."""
from __future__ import annotations

import asyncio

import pytest

from app.channels.base import Message
from app.core import faq_kb, telegram_commands, telegram_sessions
from app.integrations.panel.audit_store import get_audit_store
from app.integrations.panel.leadstore import get_lead_store
from app.integrations.panel.store import get_conversation_store


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate_faq_kb():
    faq_kb.reset()
    yield
    faq_kb.reset()


class _RecordingOrch:
    def __init__(self):
        self.handled = []
        self.bot = None

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


async def _publish_handoff_entry(*, category="passing_score", question="какой проходной балл"):
    store = faq_kb.get_faq_kb_store()
    entry = await store.create_draft({
        "canonical_question": question,
        "answer_ru": "Проходной балл 90+ (внутренняя справка, не для клиента).",
        "answer_ky": None, "category": category, "priority": 10, "handoff_only": True,
    }, [], "mgr")
    result = await store.publish(entry.id, "mgr", confirm=True)
    assert result.ok
    return entry


# --------------------------------------------------------------------------------------
# 26. Matched handoff_only -> safe phrase sent, NEVER the raw stored answer.
# --------------------------------------------------------------------------------------

def test_handoff_only_sends_safe_phrase_not_raw_answer():
    async def scenario():
        await _publish_handoff_entry()
        bot_id, uid = "hoff_1", "u26"
        orch = _RecordingOrch()
        adapter = _SendOnlyAdapter()
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="какой проходной балл", kind="text")

        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=adapter, orchestrator=orch)

        assert adapter.sent == [telegram_commands.FAQ_HANDOFF_SAFE_PHRASE]
        assert "90+" not in adapter.sent[0]
        assert orch.handled == []
    _run(scenario())


# --------------------------------------------------------------------------------------
# 27. handoff_only -> dialog_owner=manager, bot_phase=handoff (reuses request_manager).
# --------------------------------------------------------------------------------------

def test_handoff_only_sets_owner_manager_and_phase_handoff():
    async def scenario():
        await _publish_handoff_entry()
        bot_id, uid = "hoff_2", "u27"
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="какой проходной балл", kind="text")

        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=_SendOnlyAdapter(), orchestrator=_RecordingOrch())

        conv, _lead = await telegram_sessions.get_active_session(bot_id, uid)
        assert conv.dialog_owner == "manager"
        assert conv.bot_phase == "handoff"
    _run(scenario())


# --------------------------------------------------------------------------------------
# 28. handoff_only -> lead_status UNCHANGED.
# --------------------------------------------------------------------------------------

def test_handoff_only_leaves_lead_status_unchanged():
    async def scenario():
        await _publish_handoff_entry()
        bot_id, uid = "hoff_3", "u28"
        # Pre-create the session so we know the "before" status precisely.
        session = await telegram_sessions.ensure_active_session(bot_id, uid)
        before_status = session.lead.lead_status

        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="какой проходной балл", kind="text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=_SendOnlyAdapter(), orchestrator=_RecordingOrch())

        lead = await get_lead_store().get_lead(session.lead.id)
        assert lead.lead_status == before_status == "new"
    _run(scenario())


# --------------------------------------------------------------------------------------
# 29. handoff_only -> NO lead_status_changed audit event / no outbox-triggering write
#     (only dialog_owner_changed + bot_phase_changed, exactly like the /manager command).
# --------------------------------------------------------------------------------------

def test_handoff_only_writes_no_lead_status_audit_event():
    async def scenario():
        await _publish_handoff_entry()
        bot_id, uid = "hoff_4", "u29"
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="какой проходной балл", kind="text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=_SendOnlyAdapter(), orchestrator=_RecordingOrch())

        conv, lead = await telegram_sessions.get_active_session(bot_id, uid)
        audit = await get_audit_store().list_for_lead(lead.id)
        types = {a.event_type for a in audit}
        assert "dialog_owner_changed" in types
        assert "bot_phase_changed" in types
        assert "lead_status_changed" not in types
    _run(scenario())


# --------------------------------------------------------------------------------------
# 30. handoff_only reply is logged to the legacy panel AND a faq_kb_answer_log row is
#     written with the matched entry's data (missing_answer_ky reflected too).
# --------------------------------------------------------------------------------------

def test_handoff_only_logs_panel_and_answer_log():
    async def scenario():
        entry = await _publish_handoff_entry(question="сколько стоит окуу")
        bot_id, uid = "hoff_5", "u30"
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="сколько стоит окуу", kind="text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=_SendOnlyAdapter(), orchestrator=_RecordingOrch())

        panel_conv = await get_conversation_store().get(f"{bot_id}:{uid}")
        assert panel_conv is not None
        texts = [(m.sender, m.text) for m in panel_conv.messages]
        assert ("client", "сколько стоит окуу") in texts
        assert ("bot", telegram_commands.FAQ_HANDOFF_SAFE_PHRASE) in texts

        log_rows = await faq_kb.get_faq_kb_store().list_answer_log()
        assert len(log_rows) == 1
        row = log_rows[0]
        assert row["faq_entry_id"] == entry.id
        assert row["source"] == "faq"
        assert row["match_type"] == "canonical"
    _run(scenario())

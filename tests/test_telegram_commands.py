"""Increment 4: telegram pilot commands + normal-message dispatcher.

Covers scenarios 9-32 from the finish-sequence brief in
`docs/telegram-pilot-implementation-plan.md`:
commands (9-16), ignored update types (17-21), OFF/owner gating (22-27), error
handling (28-30, 28/30 mostly covered in `tests/test_telegram_sessions.py`).

Two styles:
- Unit-level: call `app.core.telegram_commands.handle_command`/`route_message`
  directly with fakes — fast, precise, no network (9-15, 22-27, 29).
- Webhook-level: `TestClient` against the real `/webhook/telegram/{bot_id}` route,
  by the same conventions as `tests/test_telegram_pilot_security.py` (16-21).
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

import app.main as m
from app.channels.base import Message
from app.config import BotConfig, TelegramBotConfig, settings
from app.core import flags, telegram_commands, telegram_sessions
from app.core.conversation_service import ConversationService
from app.core.orchestrator import Orchestrator
from app.integrations.panel.audit_store import get_audit_store
from app.integrations.panel.leadstore import get_lead_store
from app.integrations.panel.store import get_conversation_store


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------------------
# Fakes (no network) — conventions from tests/test_telegram_pilot_security.py.
# --------------------------------------------------------------------------------------

class _RecordingOrch:
    """Stands in for Orchestrator when the test only cares whether .handle() was
    called at all (LLM/FAQ gate) — NOT for tests that need the real OFF/FAQ logic."""

    def __init__(self, bot=None):
        self.handled: list[Message] = []
        self.bot = bot

    async def handle(self, msg: Message) -> None:
        self.handled.append(msg)


class _SendOnlyAdapter:
    channel = "telegram"

    def __init__(self, fail: bool = False):
        self.sent: list[str] = []
        self._fail = fail

    async def send(self, chat_id, text, **kw):
        if self._fail:
            raise RuntimeError("telegram API down (test-only)")
        self.sent.append(text)
        return None


class _WebhookFakeAdapter:
    """Mirrors TelegramAdapter.parse() for webhook-level tests (no real network)."""

    channel = "telegram"

    async def parse(self, raw):
        msg = raw.get("message") or {}
        return Message(
            channel="telegram",
            user_id=str((msg.get("from") or {}).get("id", "")),
            chat_id=str((msg.get("chat") or {}).get("id", "")),
            text=msg.get("text", ""), kind="text", raw=raw,
        )

    async def send(self, chat_id, text, **kw):
        return None


def _inject_bot(bot_id, secret=""):
    orch = _RecordingOrch()
    m._telegram_test[bot_id] = (_WebhookFakeAdapter(), orch)
    m._tg_bot_cfgs[bot_id] = TelegramBotConfig(id=bot_id, token="x:y", webhook_secret=secret)
    return orch


def _remove_bot(bot_id, *update_ids):
    m._telegram_test.pop(bot_id, None)
    m._tg_bot_cfgs.pop(bot_id, None)
    for uid in update_ids:
        m._seen_tg_ids.pop(f"{bot_id}:{uid}", None)


# --------------------------------------------------------------------------------------
# 9. Command text never reaches the orchestrator (-> never reaches LLM/FAQ).
# --------------------------------------------------------------------------------------

def test_command_not_sent_to_llm_or_faq():
    async def scenario():
        orch = _RecordingOrch()
        adapter = _SendOnlyAdapter()
        msg = Message(channel="telegram", user_id="cmd-u9", chat_id="cmd-u9", text="/status", kind="text")

        await telegram_commands.route_message(msg, bot_id="college_1", adapter=adapter, orchestrator=orch)

        assert orch.handled == []
        assert len(adapter.sent) == 1

    _run(scenario())


# --------------------------------------------------------------------------------------
# 10. /status shows SAFE state only — no secrets/tokens leak into the reply.
# --------------------------------------------------------------------------------------

def test_status_shows_safe_state_and_no_secrets():
    async def scenario():
        bot_id, uid = "college_1", "cmd-u10"
        session = await telegram_sessions.ensure_active_session(bot_id, uid)
        await get_lead_store().update_lead(
            session.lead.id, name="Данияр", grade_base="9", direction="IT",
        )

        reply = await telegram_commands.handle_command(
            bot_id=bot_id, external_user_id=uid, external_chat_id=uid, command="/status", args="",
        )

        assert "lead_status: new" in reply
        assert "bot_phase: greeting" in reply
        assert "dialog_owner: bot" in reply
        assert "Данияр" in reply
        assert "grade_base=9" in reply
        # never leak secrets
        for forbidden in ("token", "secret", "openrouter", "api_key", settings.session_secret):
            assert forbidden.lower() not in reply.lower()

    _run(scenario())


def test_status_auto_creates_session_when_none_active():
    async def scenario():
        bot_id, uid = "college_1", "cmd-u10b"
        reply = await telegram_commands.handle_command(
            bot_id=bot_id, external_user_id=uid, external_chat_id=uid, command="/status", args="",
        )
        assert "создана новая" in reply.lower()
        conv, _lead = await telegram_sessions.get_active_session(bot_id, uid)
        assert conv is not None

    _run(scenario())


# --------------------------------------------------------------------------------------
# 11. /manager changes owner+phase, NOT lead_status. Audits BOTH changes.
# --------------------------------------------------------------------------------------

def test_manager_command_changes_owner_and_phase_not_lead_status():
    async def scenario():
        bot_id, uid = "college_1", "cmd-u11"
        session = await telegram_sessions.ensure_active_session(bot_id, uid)
        before_status = session.lead.lead_status

        reply = await telegram_commands.handle_command(
            bot_id=bot_id, external_user_id=uid, external_chat_id=uid, command="/manager", args="",
        )
        assert reply

        store = get_lead_store()
        conv = await store.get_conversation(session.conversation.id)
        lead = await store.get_lead(session.lead.id)
        assert conv.dialog_owner == "manager"
        assert conv.bot_phase == "handoff"
        assert conv.assigned_to == ""            # NOT a takeover — no fictitious assignee
        assert lead.lead_status == before_status  # untouched

        audit = await get_audit_store().list_for_lead(session.lead.id)
        types = {a.event_type for a in audit}
        assert "dialog_owner_changed" in types
        assert "bot_phase_changed" in types

    _run(scenario())


# --------------------------------------------------------------------------------------
# 12. /bot -> owner=bot; handoff->consultation rule; assigned_to KEPT.
# --------------------------------------------------------------------------------------

def test_bot_command_returns_owner_to_bot_and_downgrades_handoff_phase():
    async def scenario():
        bot_id, uid = "college_1", "cmd-u12"
        session = await telegram_sessions.ensure_active_session(bot_id, uid)
        service = ConversationService()
        await service.takeover(session.conversation.id, "aidana")
        store = get_lead_store()
        await store.update_conversation(session.conversation.id, bot_phase="handoff")

        reply = await telegram_commands.handle_command(
            bot_id=bot_id, external_user_id=uid, external_chat_id=uid, command="/bot", args="",
        )
        assert reply

        conv = await store.get_conversation(session.conversation.id)
        assert conv.dialog_owner == "bot"
        assert conv.bot_phase == "consultation"   # handoff -> consultation (documented rule)
        assert conv.assigned_to == "aidana"        # KEPT (Increment-3 release rule)

    _run(scenario())


def test_bot_command_leaves_non_handoff_phase_untouched():
    async def scenario():
        bot_id, uid = "college_1", "cmd-u12b"
        session = await telegram_sessions.ensure_active_session(bot_id, uid)
        store = get_lead_store()
        await store.update_conversation(session.conversation.id, bot_phase="consultation", dialog_owner="manager")

        await telegram_commands.handle_command(
            bot_id=bot_id, external_user_id=uid, external_chat_id=uid, command="/bot", args="",
        )

        conv = await store.get_conversation(session.conversation.id)
        assert conv.dialog_owner == "bot"
        assert conv.bot_phase == "consultation"  # already non-handoff -> unchanged

    _run(scenario())


# --------------------------------------------------------------------------------------
# 13. /help lists all commands.
# --------------------------------------------------------------------------------------

def test_help_lists_all_commands():
    async def scenario():
        reply = await telegram_commands.handle_command(
            bot_id="college_1", external_user_id="cmd-u13", external_chat_id="cmd-u13",
            command="/help", args="",
        )
        for cmd in telegram_commands.COMMANDS:
            assert cmd in reply

    _run(scenario())


# --------------------------------------------------------------------------------------
# 14. Unknown command -> short help text, no LLM.
# --------------------------------------------------------------------------------------

def test_unknown_command_no_llm():
    async def scenario():
        orch = _RecordingOrch()
        adapter = _SendOnlyAdapter()
        msg = Message(channel="telegram", user_id="cmd-u14", chat_id="cmd-u14",
                     text="/frobnicate", kind="text")

        await telegram_commands.route_message(msg, bot_id="college_1", adapter=adapter, orchestrator=orch)

        assert orch.handled == []
        assert adapter.sent == [telegram_commands.UNKNOWN_COMMAND_TEXT]

    _run(scenario())


# --------------------------------------------------------------------------------------
# 15. /feedback saved as test_note, never mixed with a normal client message.
# --------------------------------------------------------------------------------------

def test_feedback_saved_as_test_note_not_client_message():
    async def scenario():
        bot_id, uid = "college_1", "cmd-u15"
        orch = _RecordingOrch()
        adapter = _SendOnlyAdapter()
        msg = Message(channel="telegram", user_id=uid, chat_id=uid,
                     text="/feedback ответ неточный", kind="text")

        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=adapter, orchestrator=orch)

        assert orch.handled == []  # never touches LLM/FAQ
        conv, lead = await telegram_sessions.get_active_session(bot_id, uid)
        assert conv is not None

        audit = await get_audit_store().list_for_lead(lead.id)
        notes = [a for a in audit if a.event_type == "test_note"]
        assert len(notes) == 1
        assert notes[0].metadata == {"comment": "ответ неточный"}
        assert notes[0].conversation_id == conv.id

        # feedback text is NEVER logged into the legacy panel as a client message.
        panel_conv = await get_conversation_store().get(f"{bot_id}:{uid}")
        assert panel_conv is None

    _run(scenario())


def test_feedback_without_text_does_not_save_empty_comment():
    async def scenario():
        bot_id, uid = "college_1", "cmd-u15b"
        reply = await telegram_commands.handle_command(
            bot_id=bot_id, external_user_id=uid, external_chat_id=uid, command="/feedback", args="   ",
        )
        assert "напишите" in reply.lower()
        conv, _lead = await telegram_sessions.get_active_session(bot_id, uid)
        assert conv is None  # never silently lost, but also never saved as empty

    _run(scenario())


# --------------------------------------------------------------------------------------
# 16. Commands are allowlist-only (webhook-level: stranger's /newtest never runs).
# --------------------------------------------------------------------------------------

def test_command_from_non_allowlisted_user_not_processed(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [9191])
    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", [])
    monkeypatch.setattr(settings, "webhook_secret", "")
    orch = _inject_bot("cmdallowbot", secret="")
    try:
        body = {"update_id": 4001, "message": {"from": {"id": 9292}, "chat": {"id": 9292}, "text": "/newtest"}}
        with TestClient(m.app) as client:
            resp = client.post("/webhook/telegram/cmdallowbot", json=body)
        assert resp.status_code == 200
        assert resp.json().get("skipped") == "not_allowed"
        assert orch.handled == []
        conv, _lead = _run(telegram_sessions.get_active_session("cmdallowbot", "9292"))
        assert conv is None  # /newtest never ran for a stranger
    finally:
        _remove_bot("cmdallowbot", 4001)


# --------------------------------------------------------------------------------------
# 17. Repeated update_id does not repeat /newtest.
# --------------------------------------------------------------------------------------

def test_repeated_update_id_does_not_repeat_newtest(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [7171])
    monkeypatch.setattr(settings, "webhook_secret", "")
    _inject_bot("dedupcmdbot", secret="")
    try:
        body = {"update_id": 9001, "message": {"from": {"id": 7171}, "chat": {"id": 7171}, "text": "/newtest"}}
        with TestClient(m.app) as client:
            r1 = client.post("/webhook/telegram/dedupcmdbot", json=body)
            conv_after_first, _lead = _run(telegram_sessions.get_active_session("dedupcmdbot", "7171"))
            assert conv_after_first is not None

            r2 = client.post("/webhook/telegram/dedupcmdbot", json=body)  # duplicate delivery
        assert r1.status_code == 200 and r2.status_code == 200
        assert r2.json().get("dedup") is True

        conv_after_second, _lead2 = _run(telegram_sessions.get_active_session("dedupcmdbot", "7171"))
        assert conv_after_second.id == conv_after_first.id  # /newtest did NOT run a second time
    finally:
        _remove_bot("dedupcmdbot", 9001)


# --------------------------------------------------------------------------------------
# 18/19. callback_query -> ignored, no Conversation, no LLM.
# --------------------------------------------------------------------------------------

def test_callback_query_ignored_no_conversation_no_llm(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [8181])
    monkeypatch.setattr(settings, "webhook_secret", "")
    orch = _inject_bot("cbbot", secret="")
    try:
        body = {
            "update_id": 3001,
            "callback_query": {
                "id": "cb1", "from": {"id": 8181},
                "message": {"chat": {"id": 8181}}, "data": "whatever",
            },
        }
        with TestClient(m.app) as client:
            resp = client.post("/webhook/telegram/cbbot", json=body)
        assert resp.status_code == 200
        assert resp.json().get("skipped") == "callback_query"
        assert orch.handled == []
        conv, _lead = _run(telegram_sessions.get_active_session("cbbot", "8181"))
        assert conv is None
    finally:
        _remove_bot("cbbot", 3001)


# --------------------------------------------------------------------------------------
# 20. edited_message ignored, not processed as a new message.
# --------------------------------------------------------------------------------------

def test_edited_message_not_processed_as_new(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [8282])
    monkeypatch.setattr(settings, "webhook_secret", "")
    orch = _inject_bot("editbot", secret="")
    try:
        body = {"update_id": 3002, "edited_message": {"from": {"id": 8282}, "chat": {"id": 8282}, "text": "изменено"}}
        with TestClient(m.app) as client:
            resp = client.post("/webhook/telegram/editbot", json=body)
        assert resp.status_code == 200
        assert resp.json().get("skipped") == "edited_message"
        assert orch.handled == []
        conv, _lead = _run(telegram_sessions.get_active_session("editbot", "8282"))
        assert conv is None
    finally:
        _remove_bot("editbot", 3002)


# --------------------------------------------------------------------------------------
# 21. Group/supergroup/channel message -> no Lead, no session.
# --------------------------------------------------------------------------------------

def test_group_message_creates_no_lead(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [8383])
    monkeypatch.setattr(settings, "webhook_secret", "")
    orch = _inject_bot("groupbot", secret="")
    try:
        body = {
            "update_id": 3003,
            "message": {"from": {"id": 8383}, "chat": {"id": -100123, "type": "group"}, "text": "привет"},
        }
        with TestClient(m.app) as client:
            resp = client.post("/webhook/telegram/groupbot", json=body)
        assert resp.status_code == 200
        assert resp.json().get("skipped") == "non_private_chat"
        assert orch.handled == []
        conv, _lead = _run(telegram_sessions.get_active_session("groupbot", "8383"))
        assert conv is None
    finally:
        _remove_bot("groupbot", 3003)


# --------------------------------------------------------------------------------------
# 22. Global OFF is not bypassed by commands (commands run, but don't flip auto-reply on).
# --------------------------------------------------------------------------------------

def test_global_off_not_bypassed_by_commands():
    async def scenario():
        flags.reset()
        await flags.set_flag("bots_enabled", False)
        bot_id, uid = "offcmd_1", "cmd-u22"
        sent: list[str] = []

        class _Ch:
            channel = "telegram"

            async def send(self, chat_id, text, **kw):
                sent.append(text)

        bot_cfg = BotConfig(id=bot_id, scenario="admission")
        orch = Orchestrator(channel=_Ch(), bot=bot_cfg)

        reply = await telegram_commands.handle_command(
            bot_id=bot_id, external_user_id=uid, external_chat_id=uid, command="/bot", args="",
        )
        assert reply  # command itself is never gated by the switch

        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="сколько стоит?", kind="text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=_Ch(), orchestrator=orch)

        assert sent == []  # commands did not flip on auto-replies
        flags.reset()

    _run(scenario())


# --------------------------------------------------------------------------------------
# 23. /status works at OFF.
# --------------------------------------------------------------------------------------

def test_status_works_when_globally_off():
    async def scenario():
        flags.reset()
        await flags.set_flag("bots_enabled", False)
        try:
            reply = await telegram_commands.handle_command(
                bot_id="college_1", external_user_id="cmd-u23", external_chat_id="cmd-u23",
                command="/status", args="",
            )
            assert "lead_status" in reply
        finally:
            flags.reset()

    _run(scenario())


# --------------------------------------------------------------------------------------
# 24. At OFF: normal message is saved, LLM/auto-reply not called.
# --------------------------------------------------------------------------------------

def test_off_saves_normal_message_without_calling_llm():
    async def scenario():
        flags.reset()
        await flags.set_flag("bots_enabled", False)
        bot_id, uid = "offcmd_2", "cmd-u24"
        sent: list[str] = []

        class _Ch:
            channel = "telegram"

            async def send(self, chat_id, text, **kw):
                sent.append(text)

        bot_cfg = BotConfig(id=bot_id, scenario="admission")
        orch = Orchestrator(channel=_Ch(), bot=bot_cfg)
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="сколько стоит?", kind="text")

        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=_Ch(), orchestrator=orch)

        assert sent == []
        panel_conv = await get_conversation_store().get(f"{bot_id}:{uid}")
        assert panel_conv is not None
        assert any(msg_.sender == "client" and msg_.text == "сколько стоит?" for msg_ in panel_conv.messages)
        flags.reset()

    _run(scenario())


# --------------------------------------------------------------------------------------
# 25. manager owner blocks auto-reply.
# --------------------------------------------------------------------------------------

def test_manager_owner_blocks_auto_reply():
    async def scenario():
        bot_id, uid = "college_1", "cmd-u25"
        session = await telegram_sessions.ensure_active_session(bot_id, uid)
        await ConversationService().request_manager(session.conversation.id)

        orch = _RecordingOrch()
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="какие направления?", kind="text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=_SendOnlyAdapter(), orchestrator=orch)

        assert orch.handled == []  # bot never even sees it
        panel_conv = await get_conversation_store().get(f"{bot_id}:{uid}")
        assert panel_conv is not None
        assert any(m_.text == "какие направления?" for m_ in panel_conv.messages)  # still visible to admin

    _run(scenario())


# --------------------------------------------------------------------------------------
# 26. paused owner blocks auto-reply.
# --------------------------------------------------------------------------------------

def test_paused_owner_blocks_auto_reply():
    async def scenario():
        bot_id, uid = "college_1", "cmd-u26"
        session = await telegram_sessions.ensure_active_session(bot_id, uid)
        await ConversationService().pause(session.conversation.id)

        orch = _RecordingOrch()
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="привет", kind="text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=_SendOnlyAdapter(), orchestrator=orch)

        assert orch.handled == []
        panel_conv = await get_conversation_store().get(f"{bot_id}:{uid}")
        assert panel_conv is not None

    _run(scenario())


# --------------------------------------------------------------------------------------
# 27. /bot at global OFF does not enable AI.
# --------------------------------------------------------------------------------------

def test_bot_command_at_global_off_does_not_enable_ai():
    async def scenario():
        flags.reset()
        await flags.set_flag("bots_enabled", False)
        bot_id, uid = "offcmd_3", "cmd-u27"
        sent: list[str] = []

        class _Ch:
            channel = "telegram"

            async def send(self, chat_id, text, **kw):
                sent.append(text)

        bot_cfg = BotConfig(id=bot_id, scenario="admission")
        orch = Orchestrator(channel=_Ch(), bot=bot_cfg)

        await telegram_commands.handle_command(
            bot_id=bot_id, external_user_id=uid, external_chat_id=uid, command="/manager", args="",
        )
        await telegram_commands.handle_command(
            bot_id=bot_id, external_user_id=uid, external_chat_id=uid, command="/bot", args="",
        )

        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="хочу приехать", kind="text")
        await telegram_commands.route_message(msg, bot_id=bot_id, adapter=_Ch(), orchestrator=orch)

        assert sent == []  # /bot brought owner back, but global OFF still silences the bot
        flags.reset()

    _run(scenario())


# --------------------------------------------------------------------------------------
# 29. Telegram send failure for a command reply does NOT roll back the created session.
# --------------------------------------------------------------------------------------

def test_command_reply_send_failure_keeps_created_session():
    async def scenario():
        bot_id, uid = "college_1", "cmd-u29"
        orch = _RecordingOrch()
        msg = Message(channel="telegram", user_id=uid, chat_id=uid, text="/newtest", kind="text")

        # Should not raise — send failures are caught and logged, not propagated.
        await telegram_commands.route_message(
            msg, bot_id=bot_id, adapter=_SendOnlyAdapter(fail=True), orchestrator=orch,
        )

        conv, _lead = await telegram_sessions.get_active_session(bot_id, uid)
        assert conv is not None  # session persisted despite the send failure

    _run(scenario())

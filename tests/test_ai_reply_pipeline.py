"""Increment 6: app/core/ai_reply.py full pipeline (brief §20 scenarios 10-13 one-call,
33-40 classification, 41-43 takeover race, 52-57 errors, 58-60 regression).

`ai_reply._llm_caller` is monkeypatched to a fake coroutine returning a canned
`StructuredCallResult` (or raising) — NO real network call is ever made. Each test
builds its own bot_id/user_id pair so sessions never collide across tests (matches the
convention in tests/test_telegram_commands.py / tests/test_faq_kb_gates.py).
"""
from __future__ import annotations

import asyncio

import pytest

from app.agent.structured_llm import StructuredCallResult, UsageInfo
from app.channels.base import Message
from app.config import settings
from app.core import ai_reply, faq_kb, flags, pilot_prompt, telegram_sessions
from app.core.conversation_service import ConversationService
from app.integrations.panel.ai_log_store import get_ai_log_store, reset as reset_ai_log
from app.integrations.panel.leadstore import get_lead_store


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    faq_kb.reset()
    flags.reset()
    reset_ai_log()
    monkeypatch.setattr(settings, "llm_daily_budget_usd", 0.0)
    monkeypatch.setattr(settings, "llm_monthly_budget_usd", 0.0)
    monkeypatch.setattr(settings, "ai_status_confidence_threshold", 0.90)
    monkeypatch.setattr(settings, "llm_model_main", "anthropic/claude-haiku-4.5")
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
        return "provider-msg-1"


def _classification(**overrides):
    base = {
        "intent": "asks_general_info", "confidence": 0.95, "evidence": "клиент спросил про поступление",
        "lead_temperature": "warm", "suggested_status": "in_progress", "next_action_type": None,
        "next_action_at": None, "should_handoff": False, "handoff_reason": None,
        "qualification_updates": {},
    }
    base.update(overrides)
    return base


def _arguments(reply="Здравствуйте! Расскажите, после какого класса поступаете?", **overrides):
    payload = {
        "reply": reply, "language": "ru",
        "answer_basis": {"knowledge_entry_ids": [], "facts_used": []},
        "classification": _classification(**(overrides.pop("classification", None) or {})),
        "summary_update": "Клиент интересуется поступлением.",
        "safety": {"uncertain": False, "unsupported_claims": [], "requires_human_confirmation": False},
    }
    payload.update(overrides)
    return payload


def _fake_caller(arguments=None, *, ok=True, error=None, usage=None):
    async def _caller(*, system, messages, model, max_output_tokens, timeout_seconds):
        if not ok:
            return StructuredCallResult(ok=False, error=error, model=model, latency_ms=5.0, retry_count=0)
        return StructuredCallResult(
            ok=True, arguments=arguments or _arguments(), model=model, latency_ms=42.0,
            retry_count=0, generation_id="gen-test",
            usage=usage or UsageInfo(input_tokens=100, output_tokens=40, total_tokens=140, cost=0.00042, cost_source="provider"),
        )
    return _caller


async def _new_session(bot_id: str, uid: str):
    session = await telegram_sessions.ensure_active_session(bot_id, uid, external_chat_id=uid)
    return session


def _msg(uid: str, text: str, kind: str = "text") -> Message:
    return Message(channel="telegram", user_id=uid, chat_id=uid, text=text, kind=kind)


# --------------------------------------------------------------------------------------
# 10-13. One structured call.
# --------------------------------------------------------------------------------------

def test_reply_sent_and_usage_context_logged(monkeypatch):
    async def scenario():
        bot_id, uid = "pipe1", "u1"
        session = await _new_session(bot_id, uid)
        monkeypatch.setattr(ai_reply, "_llm_caller", _fake_caller())
        adapter = _SendOnlyAdapter()

        outcome = await ai_reply.generate_and_send_reply(
            _msg(uid, "хочу узнать о поступлении"), bot_id=bot_id, adapter=adapter,
            orchestrator=_RecordingOrch(), session=session,
        )
        assert outcome == "sent"
        assert adapter.sent == ["Здравствуйте! Расскажите, после какого класса поступаете?"]

        rows = await get_ai_log_store().list_for_conversation(session.conversation.id)
        assert len(rows) == 1
        row = rows[0]
        assert row.outcome == "sent"
        assert row.model == "anthropic/claude-haiku-4.5"
        assert row.prompt_version == pilot_prompt.PROMPT_VERSION
        assert row.input_tokens == 100 and row.output_tokens == 40
        assert row.cost == pytest.approx(0.00042)
        assert row.cost_source == "provider"
        assert row.intent == "asks_general_info"
        assert row.confidence == pytest.approx(0.95)
        assert row.dialog_owner == "bot"
    _run(scenario())


def test_only_one_network_call_for_one_turn(monkeypatch):
    calls = {"n": 0}

    async def _counting_caller(**kwargs):
        calls["n"] += 1
        return StructuredCallResult(
            ok=True, arguments=_arguments(), model=kwargs["model"], latency_ms=1.0,
            usage=UsageInfo(input_tokens=10, output_tokens=10, total_tokens=20, cost=0.0001, cost_source="provider"),
        )

    async def scenario():
        bot_id, uid = "pipe2", "u2"
        session = await _new_session(bot_id, uid)
        monkeypatch.setattr(ai_reply, "_llm_caller", _counting_caller)
        await ai_reply.generate_and_send_reply(
            _msg(uid, "привет"), bot_id=bot_id, adapter=_SendOnlyAdapter(),
            orchestrator=_RecordingOrch(), session=session,
        )
        assert calls["n"] == 1
    _run(scenario())


# --------------------------------------------------------------------------------------
# 33-40. Classification -> status/qualification application.
# --------------------------------------------------------------------------------------

def test_high_confidence_status_applied_via_lead_status_service(monkeypatch):
    async def scenario():
        bot_id, uid = "pipe3", "u3"
        session = await _new_session(bot_id, uid)
        monkeypatch.setattr(ai_reply, "_llm_caller", _fake_caller(_arguments(
            classification=_classification(suggested_status="in_progress", confidence=0.95),
        )))
        await ai_reply.generate_and_send_reply(
            _msg(uid, "хочу узнать о поступлении"), bot_id=bot_id, adapter=_SendOnlyAdapter(),
            orchestrator=_RecordingOrch(), session=session,
        )
        lead = await get_lead_store().get_lead(session.lead.id)
        assert lead.lead_status == "in_progress"
    _run(scenario())


def test_low_confidence_kept_as_suggestion_only(monkeypatch):
    async def scenario():
        bot_id, uid = "pipe4", "u4"
        session = await _new_session(bot_id, uid)
        monkeypatch.setattr(ai_reply, "_llm_caller", _fake_caller(_arguments(
            classification=_classification(suggested_status="in_progress", confidence=0.4),
        )))
        await ai_reply.generate_and_send_reply(
            _msg(uid, "хочу узнать о поступлении"), bot_id=bot_id, adapter=_SendOnlyAdapter(),
            orchestrator=_RecordingOrch(), session=session,
        )
        lead = await get_lead_store().get_lead(session.lead.id)
        assert lead.lead_status == "new"                # untouched
        assert lead.suggested_status == "in_progress"    # recorded as suggestion
    _run(scenario())


def test_manager_only_status_never_auto_applied(monkeypatch):
    async def scenario():
        bot_id, uid = "pipe5", "u5"
        session = await _new_session(bot_id, uid)
        monkeypatch.setattr(ai_reply, "_llm_caller", _fake_caller(_arguments(
            reply="Понимаю, спасибо за то, что сообщили — если что, мы всегда рады помочь.",
            classification=_classification(
                intent="explicit_rejection", suggested_status="rejected", confidence=0.99,
                should_handoff=True, handoff_reason="клиент уже поступил в другой колледж",
            ),
        )))
        session2 = session
        await ai_reply.generate_and_send_reply(
            _msg(uid, "мы уже поступили в другой колледж"), bot_id=bot_id, adapter=_SendOnlyAdapter(),
            orchestrator=_RecordingOrch(), session=session2,
        )
        lead = await get_lead_store().get_lead(session.lead.id)
        assert lead.lead_status == "new"          # bot NEVER applies rejected
        assert lead.suggested_status == "rejected"  # recorded for the manager to confirm

        conv = await get_lead_store().get_conversation(session.conversation.id)
        assert conv.dialog_owner == "manager"      # should_handoff=True -> handoff fired
    _run(scenario())


def test_wants_to_visit_uses_atomic_invited_handoff(monkeypatch):
    async def scenario():
        bot_id, uid = "pipe6", "u6"
        session = await _new_session(bot_id, uid)
        monkeypatch.setattr(ai_reply, "_llm_caller", _fake_caller(_arguments(
            reply="Записал ваше желание прийти — точное время подтвердит менеджер.",
            classification=_classification(
                intent="wants_to_visit", suggested_status="invited", confidence=0.97,
                should_handoff=True, handoff_reason="клиент готов прийти",
            ),
        )))
        await ai_reply.generate_and_send_reply(
            _msg(uid, "хочу записаться на тест"), bot_id=bot_id, adapter=_SendOnlyAdapter(),
            orchestrator=_RecordingOrch(), session=session,
        )
        lead = await get_lead_store().get_lead(session.lead.id)
        conv = await get_lead_store().get_conversation(session.conversation.id)
        assert lead.lead_status == "invited"
        assert conv.bot_phase == "handoff"
        assert conv.dialog_owner == "manager"
    _run(scenario())


def test_qualification_updates_applied_when_previously_empty(monkeypatch):
    async def scenario():
        bot_id, uid = "pipe7", "u7"
        session = await _new_session(bot_id, uid)
        monkeypatch.setattr(ai_reply, "_llm_caller", _fake_caller(_arguments(
            reply="Приятно познакомиться, Данияр! После какого класса поступаете?",
            classification=_classification(
                intent="provides_name", suggested_status=None, confidence=0.95,
                qualification_updates={"name": "Данияр"},
            ),
        )))
        await ai_reply.generate_and_send_reply(
            _msg(uid, "меня зовут Данияр"), bot_id=bot_id, adapter=_SendOnlyAdapter(),
            orchestrator=_RecordingOrch(), session=session,
        )
        lead = await get_lead_store().get_lead(session.lead.id)
        assert lead.name == "Данияр"
    _run(scenario())


def test_qualification_never_overwrites_confirmed_value_with_low_confidence(monkeypatch):
    async def scenario():
        bot_id, uid = "pipe8", "u8"
        session = await _new_session(bot_id, uid)
        await get_lead_store().update_lead(session.lead.id, name="Данияр")

        monkeypatch.setattr(ai_reply, "_llm_caller", _fake_caller(_arguments(
            classification=_classification(
                intent="provides_name", suggested_status=None, confidence=0.3,
                qualification_updates={"name": "Опечатка"},
            ),
        )))
        await ai_reply.generate_and_send_reply(
            _msg(uid, "..."), bot_id=bot_id, adapter=_SendOnlyAdapter(),
            orchestrator=_RecordingOrch(), session=session,
        )
        lead = await get_lead_store().get_lead(session.lead.id)
        assert lead.name == "Данияр"   # NOT overwritten by a low-confidence guess
    _run(scenario())


def test_callback_requested_applies_callback_status_and_next_action(monkeypatch):
    async def scenario():
        bot_id, uid = "pipe9", "u9"
        session = await _new_session(bot_id, uid)
        monkeypatch.setattr(ai_reply, "_llm_caller", _fake_caller(_arguments(
            reply="Хорошо, передам ваш запрос менеджеру — он свяжется с вами.",
            classification=_classification(
                intent="callback_requested", suggested_status="callback", confidence=0.97,
                next_action_type="callback", next_action_at="2026-07-20T15:00:00+06:00",
                should_handoff=True, handoff_reason="клиент попросил перезвонить",
            ),
        )))
        await ai_reply.generate_and_send_reply(
            _msg(uid, "перезвоните мне завтра после обеда"), bot_id=bot_id, adapter=_SendOnlyAdapter(),
            orchestrator=_RecordingOrch(), session=session,
        )
        lead = await get_lead_store().get_lead(session.lead.id)
        assert lead.lead_status == "callback"
        assert lead.next_action_type == "callback"
        assert lead.next_action_at is not None
    _run(scenario())


# --------------------------------------------------------------------------------------
# 41-43. Takeover re-check race.
# --------------------------------------------------------------------------------------

def test_takeover_mid_generation_drops_reply_and_skips_status(monkeypatch):
    async def scenario():
        bot_id, uid = "pipe10", "u10"
        session = await _new_session(bot_id, uid)

        async def _caller_that_races(**kwargs):
            # Simulate a manager clicking "Перехватить" WHILE the model is "generating".
            await ConversationService().takeover(session.conversation.id, "manager_ivan")
            return StructuredCallResult(
                ok=True, arguments=_arguments(classification=_classification(suggested_status="in_progress")),
                model=kwargs["model"], latency_ms=1.0,
                usage=UsageInfo(input_tokens=10, output_tokens=10, total_tokens=20, cost=0.0001, cost_source="provider"),
            )
        monkeypatch.setattr(ai_reply, "_llm_caller", _caller_that_races)
        adapter = _SendOnlyAdapter()

        outcome = await ai_reply.generate_and_send_reply(
            _msg(uid, "привет"), bot_id=bot_id, adapter=adapter, orchestrator=_RecordingOrch(), session=session,
        )
        assert outcome == "cancelled_by_takeover"
        assert adapter.sent == []   # reply NEVER sent
        lead = await get_lead_store().get_lead(session.lead.id)
        assert lead.lead_status == "new"   # status NOT applied
    _run(scenario())


def test_effective_off_mid_generation_drops_reply(monkeypatch):
    async def scenario():
        bot_id, uid = "pipe11", "u11"
        session = await _new_session(bot_id, uid)

        class _OrchGoesOff:
            bot = None

            def __init__(self):
                self._on = True

            async def _bots_on(self):
                return self._on

        orch = _OrchGoesOff()

        async def _caller_that_flips_off(**kwargs):
            orch._on = False
            return StructuredCallResult(
                ok=True, arguments=_arguments(), model=kwargs["model"], latency_ms=1.0,
                usage=UsageInfo(input_tokens=10, output_tokens=10, total_tokens=20, cost=0.0001, cost_source="provider"),
            )
        monkeypatch.setattr(ai_reply, "_llm_caller", _caller_that_flips_off)
        adapter = _SendOnlyAdapter()

        outcome = await ai_reply.generate_and_send_reply(
            _msg(uid, "привет"), bot_id=bot_id, adapter=adapter, orchestrator=orch, session=session,
        )
        assert outcome == "cancelled_by_takeover"
        assert adapter.sent == []
    _run(scenario())


def test_archived_session_mid_generation_drops_reply(monkeypatch):
    async def scenario():
        bot_id, uid = "pipe12", "u12"
        session = await _new_session(bot_id, uid)

        async def _caller_that_archives(**kwargs):
            await get_lead_store().archive_conversation(session.conversation.id)
            return StructuredCallResult(
                ok=True, arguments=_arguments(), model=kwargs["model"], latency_ms=1.0,
                usage=UsageInfo(input_tokens=10, output_tokens=10, total_tokens=20, cost=0.0001, cost_source="provider"),
            )
        monkeypatch.setattr(ai_reply, "_llm_caller", _caller_that_archives)
        adapter = _SendOnlyAdapter()

        outcome = await ai_reply.generate_and_send_reply(
            _msg(uid, "привет"), bot_id=bot_id, adapter=adapter, orchestrator=_RecordingOrch(), session=session,
        )
        assert outcome == "cancelled_by_takeover"
        assert adapter.sent == []
    _run(scenario())


# --------------------------------------------------------------------------------------
# 52-57. Errors.
# --------------------------------------------------------------------------------------

def test_timeout_error_sends_honest_fallback_no_handoff(monkeypatch):
    async def scenario():
        bot_id, uid = "pipe13", "u13"
        session = await _new_session(bot_id, uid)
        monkeypatch.setattr(ai_reply, "_llm_caller", _fake_caller(ok=False, error="timeout"))
        adapter = _SendOnlyAdapter()

        outcome = await ai_reply.generate_and_send_reply(
            _msg(uid, "привет"), bot_id=bot_id, adapter=adapter, orchestrator=_RecordingOrch(), session=session,
        )
        assert outcome == "timeout"
        assert adapter.sent == [ai_reply.TECHNICAL_ERROR_FALLBACK]
        assert "вернусь" not in adapter.sent[0].lower()   # never promises an unfulfilled callback
        conv = await get_lead_store().get_conversation(session.conversation.id)
        assert conv.dialog_owner == "bot"   # no auto-handoff for a mere technical error
    _run(scenario())


def test_schema_error_from_structured_llm_triggers_handoff(monkeypatch):
    async def scenario():
        bot_id, uid = "pipe14", "u14"
        session = await _new_session(bot_id, uid)
        monkeypatch.setattr(ai_reply, "_llm_caller", _fake_caller(ok=False, error="no_tool_call"))
        adapter = _SendOnlyAdapter()

        outcome = await ai_reply.generate_and_send_reply(
            _msg(uid, "привет"), bot_id=bot_id, adapter=adapter, orchestrator=_RecordingOrch(), session=session,
        )
        assert outcome == "schema_error"
        conv = await get_lead_store().get_conversation(session.conversation.id)
        assert conv.dialog_owner == "manager"
    _run(scenario())


def test_pydantic_validation_error_never_sends_raw_and_hands_off(monkeypatch):
    async def scenario():
        bot_id, uid = "pipe15", "u15"
        session = await _new_session(bot_id, uid)
        bad_args = _arguments()
        bad_args["classification"]["intent"] = "not_a_real_intent"
        monkeypatch.setattr(ai_reply, "_llm_caller", _fake_caller(bad_args))
        adapter = _SendOnlyAdapter()

        outcome = await ai_reply.generate_and_send_reply(
            _msg(uid, "привет"), bot_id=bot_id, adapter=adapter, orchestrator=_RecordingOrch(), session=session,
        )
        assert outcome == "schema_error"
        assert adapter.sent == [ai_reply.pilot_validator.SAFE_FALLBACK_TEXT]
        conv = await get_lead_store().get_conversation(session.conversation.id)
        assert conv.dialog_owner == "manager"
    _run(scenario())


def test_validator_blocked_never_sends_raw_reply(monkeypatch):
    async def scenario():
        bot_id, uid = "pipe16", "u16"
        session = await _new_session(bot_id, uid)
        monkeypatch.setattr(ai_reply, "_llm_caller", _fake_caller(_arguments(
            reply="Стоимость обучения 9999 долларов в год.",
        )))
        adapter = _SendOnlyAdapter()

        outcome = await ai_reply.generate_and_send_reply(
            _msg(uid, "сколько стоит?"), bot_id=bot_id, adapter=adapter, orchestrator=_RecordingOrch(), session=session,
        )
        assert outcome == "validator_blocked"
        assert adapter.sent == [ai_reply.pilot_validator.SAFE_FALLBACK_TEXT]
        assert "9999" not in "".join(adapter.sent)   # raw fabricated fact NEVER sent
        conv = await get_lead_store().get_conversation(session.conversation.id)
        assert conv.dialog_owner == "manager"
        rows = await get_ai_log_store().list_for_conversation(session.conversation.id)
        assert rows[0].outcome == "validator_blocked"
        assert "admission_price_mismatch" in rows[0].validator_violations
    _run(scenario())


def test_budget_exhausted_before_network_call_no_llm_invoked(monkeypatch):
    calls = {"n": 0}

    async def _should_never_be_called(**kwargs):
        calls["n"] += 1
        raise AssertionError("LLM must not be called when budget is exhausted")

    async def scenario():
        bot_id, uid = "pipe17", "u17"
        session = await _new_session(bot_id, uid)
        row = await get_ai_log_store().reserve(
            request_id="pre", conversation_id=session.conversation.id, lead_id=session.lead.id,
            bot_id=bot_id, model="m", prompt_version="v",
        )
        await get_ai_log_store().finalize(row.id, outcome="sent", cost=100.0)
        monkeypatch.setattr(settings, "llm_daily_budget_usd", 1.0)
        monkeypatch.setattr(ai_reply, "_llm_caller", _should_never_be_called)
        adapter = _SendOnlyAdapter()

        outcome = await ai_reply.generate_and_send_reply(
            _msg(uid, "привет"), bot_id=bot_id, adapter=adapter, orchestrator=_RecordingOrch(), session=session,
        )
        assert outcome == "budget_exhausted"
        assert calls["n"] == 0
        assert adapter.sent == [ai_reply.BUDGET_EXHAUSTED_FALLBACK]
    _run(scenario())


# --------------------------------------------------------------------------------------
# 58-60. Regression: knowledge retrieval / validator / budget integration end to end.
# --------------------------------------------------------------------------------------

def test_sourced_fact_from_published_faq_passes_validator(monkeypatch):
    async def scenario():
        bot_id, uid = "pipe18", "u18"
        store = faq_kb.get_faq_kb_store()
        entry = await store.create_draft(
            {"canonical_question": "сколько стоит обучение", "answer_ru": "Стоимость 6500 долларов в год.",
             "category": "tuition", "priority": 10, "handoff_only": False},
            [{"text": "сколько стоит обучение"}], "mgr",
        )
        await store.publish(entry.id, "mgr", confirm=True)

        session = await _new_session(bot_id, uid)
        monkeypatch.setattr(ai_reply, "_llm_caller", _fake_caller(_arguments(
            reply="Стоимость обучения 6500 долларов в год.",
            answer_basis={
                "knowledge_entry_ids": [entry.id],
                "facts_used": [{"field": "tuition", "value": "6500 долларов в год", "source_entry_id": entry.id}],
            },
        )))
        adapter = _SendOnlyAdapter()
        outcome = await ai_reply.generate_and_send_reply(
            _msg(uid, "сколько стоит обучение"), bot_id=bot_id, adapter=adapter,
            orchestrator=_RecordingOrch(), session=session,
        )
        assert outcome == "sent"
        assert adapter.sent == ["Стоимость обучения 6500 долларов в год."]
    _run(scenario())


def test_summary_update_saved_and_capped(monkeypatch):
    async def scenario():
        bot_id, uid = "pipe19", "u19"
        session = await _new_session(bot_id, uid)
        monkeypatch.setattr(ai_reply, "_llm_caller", _fake_caller(_arguments(
            summary_update="Клиент интересуется поступлением после 9 класса.",
        )))
        await ai_reply.generate_and_send_reply(
            _msg(uid, "хочу поступить после 9"), bot_id=bot_id, adapter=_SendOnlyAdapter(),
            orchestrator=_RecordingOrch(), session=session,
        )
        lead = await get_lead_store().get_lead(session.lead.id)
        assert lead.ai_summary == "Клиент интересуется поступлением после 9 класса."
    _run(scenario())

"""Increment 7: regression guards (brief scenarios 51-54). The full existing suite
staying green is verified by running `python -m pytest -q` as a whole (443 baseline +
every Increment 7 test); this file covers four SPECIFIC regressions that are easy to
silently reintroduce: secrets/PII never leaking into callback_data, the global
`telegram_feedback_enabled` kill switch actually gating every bot, the feature staying
confined to the Telegram-pilot multi-bot route (never touching the legacy/production
`Orchestrator.handle` path), and `TelegramAdapter.send`'s Increment-6 return-value
contract staying intact now that it also accepts `reply_markup`."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.channels.base import Message
from app.channels.telegram import TelegramAdapter
from app.config import BotConfig, settings
from app.core import faq_kb, telegram_commands, telegram_sessions
from app.core.feedback_service import build_feedback_keyboard
from app.core.orchestrator import Orchestrator
from app.integrations.panel.answer_context_store import get_answer_context_store, reset as reset_ctx


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate():
    faq_kb.reset()
    reset_ctx()
    yield
    faq_kb.reset()
    reset_ctx()


class _RecordingAdapter:
    channel = "telegram"

    def __init__(self):
        self.sent: list[tuple[str, str, dict | None]] = []

    async def send(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((chat_id, text, reply_markup))
        return "pmid"

    async def answer_callback(self, callback_query_id, text="", *, show_alert=False):
        return None


class _RecordingOrch:
    def __init__(self):
        self.bot = None

    async def _bots_on(self):
        return True


async def _publish_faq_entry(question="сколько стоит", answer="6500$/год"):
    store = faq_kb.get_faq_kb_store()
    entry = await store.create_draft({
        "canonical_question": question, "answer_ru": answer, "answer_ky": None,
        "category": "tuition", "priority": 10, "handoff_only": False,
    }, [], "mgr")
    result = await store.publish(entry.id, "mgr", confirm=True)
    assert result.ok
    return entry


def _msg(uid: str, text: str) -> Message:
    return Message(channel="telegram", user_id=uid, chat_id=uid, text=text, kind="text")


# 51. No secrets/tokens/webhook-secret-shaped values ever end up in callback_data,
# across a real end-to-end FAQ send.
def test_no_secrets_in_callback_data_end_to_end(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_feedback_enabled", True)
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [5101])
        monkeypatch.setattr(settings, "webhook_secret", "super-secret-value")
        monkeypatch.setattr(settings, "openrouter_api_key", "sk-should-never-leak")
        await _publish_faq_entry()
        adapter = _RecordingAdapter()
        await telegram_commands.route_message(
            _msg("5101", "сколько стоит"), bot_id="reg51", adapter=adapter, orchestrator=_RecordingOrch(),
        )
        _chat, _text, markup = adapter.sent[0]
        assert markup is not None
        blob = str(markup)
        for forbidden in ("super-secret-value", "sk-should-never-leak", settings.session_secret):
            assert forbidden not in blob
        for row in markup["inline_keyboard"]:
            for btn in row:
                assert len(btn["callback_data"].encode("utf-8")) < 64
    _run(scenario())


# 52. telegram_feedback_enabled=False is a GLOBAL kill switch — no bot shows buttons,
# even for a fully allowlisted tester with a real FAQ match.
def test_global_feedback_disabled_hides_buttons_for_every_bot(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_feedback_enabled", False)
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", [5201, 5202])
        await _publish_faq_entry()
        for bot_id, uid in (("reg52a", "5201"), ("reg52b", "5202")):
            adapter = _RecordingAdapter()
            await telegram_commands.route_message(
                _msg(uid, "сколько стоит"), bot_id=bot_id, adapter=adapter, orchestrator=_RecordingOrch(),
            )
            assert adapter.sent[0][2] is None
    _run(scenario())


# 53. The legacy/production Orchestrator.handle() path (Bitrix/WhatsApp, and the
# single-bot /webhook/telegram fallback) NEVER creates an answer_context row — the
# feature is confined to the pilot multi-bot route (telegram_commands.route_message).
def test_legacy_orchestrator_path_never_creates_answer_context(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "telegram_feedback_enabled", True)
        sent = []

        class _Ch:
            channel = "telegram"

            async def send(self, chat_id, text, **kw):
                sent.append(text)
                return None

        bot_cfg = BotConfig(id="legacy_bot", scenario="admission")
        orch = Orchestrator(channel=_Ch(), bot=bot_cfg)
        msg = Message(channel="telegram", user_id="legacy-user", chat_id="legacy-user",
                      text="сколько стоит обучение", kind="text")
        await orch.handle(msg)

        # The memory answer_context store must have received NOTHING from this path.
        store = get_answer_context_store()
        assert store._rows == {}  # type: ignore[attr-defined]
    _run(scenario())


# 54. TelegramAdapter.send keeps its Increment-6 contract (returns the provider
# message_id as a string, or None) with the new `reply_markup` parameter defaulting to
# None and converted only when given — a real aiogram Bot is mocked, no network call.
def test_telegram_adapter_send_contract_preserved_with_reply_markup(monkeypatch):
    async def scenario():
        adapter = TelegramAdapter(token="123:fake-token-not-real")
        fake_sent = type("Sent", (), {"message_id": 555})()
        mock_send_message = AsyncMock(return_value=fake_sent)
        monkeypatch.setattr(adapter._bot, "send_message", mock_send_message)

        # Plain send (no keyboard) — unchanged contract.
        result = await adapter.send("42", "hello")
        assert result == "555"
        assert mock_send_message.call_args.kwargs["reply_markup"] is None

        # With a feedback keyboard — converted to an aiogram InlineKeyboardMarkup.
        markup = build_feedback_keyboard("tok123456789")
        result2 = await adapter.send("42", "hello", reply_markup=markup)
        assert result2 == "555"
        from aiogram.types import InlineKeyboardMarkup
        sent_markup = mock_send_message.call_args.kwargs["reply_markup"]
        assert isinstance(sent_markup, InlineKeyboardMarkup)
        # Increment 7.1: 4 rows (quality x3, strategy 2+2, comment on its own row).
        assert len(sent_markup.inline_keyboard) == 4
        assert sent_markup.inline_keyboard[3][0].callback_data == "fb:tok123456789:cmt"

        mock_answer = AsyncMock(return_value=True)
        monkeypatch.setattr(adapter._bot, "answer_callback_query", mock_answer)
        await adapter.answer_callback("cbid", "Оценка сохранена.")
        mock_answer.assert_awaited_once()
    _run(scenario())

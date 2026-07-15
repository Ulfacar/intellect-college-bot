"""Инкремент 1: безопасность Telegram-пилота — allowlist, дедуп, per-bot секрет, OFF-гейт."""
import asyncio

from fastapi.testclient import TestClient

import app.main as m
from app.channels.base import Message
from app.config import BotConfig, TelegramBotConfig, settings
from app.core import allowlist


# ---- allowlist (закрыт по умолчанию) ----

def test_allowlist_closed_by_default(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [])
    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", [])
    assert allowlist.is_allowed(123, 456) is False


def test_allowlist_allows_configured_user(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [111])
    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", [])
    assert allowlist.is_allowed(111, 999) is True
    assert allowlist.is_allowed(222, 999) is False


def test_allowlist_allows_configured_chat(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [])
    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", [555])
    assert allowlist.is_allowed(222, 555) is True


# ---- дедуп по <bot_id>:<update_id> ----

def test_tg_dedup_key():
    key = "botX:987654"
    m._seen_tg_ids.pop(key, None)
    assert m._tg_seen_before(key) is False   # первый раз — новый
    assert m._tg_seen_before(key) is True    # повтор — дубль
    # без update_id не дедупим
    assert m._tg_seen_before("botX:None") is False
    assert m._tg_seen_before("botX:None") is False


# ---- вебхук: unknown bot / секрет / allowlist / дедуп ----

class _RecordingOrch:
    def __init__(self):
        self.handled = []

    async def handle(self, msg):
        self.handled.append(msg)


class _FakeAdapter:
    channel = "telegram"

    async def parse(self, raw):
        msg = raw.get("message") or {}
        return Message(channel="telegram",
                       user_id=str((msg.get("from") or {}).get("id", "")),
                       chat_id=str((msg.get("chat") or {}).get("id", "")),
                       text=msg.get("text", ""), kind="text", raw=raw)

    async def send(self, chat_id, text, **kw):
        return None


def _inject_bot(bot_id, secret=""):
    orch = _RecordingOrch()
    m._telegram_test[bot_id] = (_FakeAdapter(), orch)
    m._tg_bot_cfgs[bot_id] = TelegramBotConfig(id=bot_id, token="x:y", webhook_secret=secret)
    return orch


def _remove_bot(bot_id, *update_ids):
    m._telegram_test.pop(bot_id, None)
    m._tg_bot_cfgs.pop(bot_id, None)
    for uid in update_ids:
        m._seen_tg_ids.pop(f"{bot_id}:{uid}", None)


def test_unknown_bot_id_not_accepted():
    with TestClient(m.app) as client:
        resp = client.post("/webhook/telegram/ghost",
                           json={"update_id": 1, "message": {"text": "hi"}})
    assert resp.status_code == 404
    assert resp.json()["reason"] == "unknown_bot"


async def _fake_generate_and_send_reply(msg, *, bot_id, adapter, orchestrator, session):
    """Increment 6: `orchestrator.handle` is no longer called for normal messages —
    `app/core/ai_reply.py::generate_and_send_reply` replaced it (see
    `app/core/telegram_commands.py`). These security/gating tests only care whether the
    pipeline was reached at all (exactly once), not about LLM specifics — reuse
    `_RecordingOrch.handled` as that signal by patching the real entry point."""
    orchestrator.handled.append(msg)
    return "sent"


def test_per_bot_secret_required(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [777])
    monkeypatch.setattr("app.core.ai_reply.generate_and_send_reply", _fake_generate_and_send_reply)
    orch = _inject_bot("secbot", secret="topsecret")
    try:
        with TestClient(m.app) as client:
            bad = client.post("/webhook/telegram/secbot",
                              headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
                              json={"update_id": 100, "message": {"from": {"id": 777},
                                    "chat": {"id": 777}, "text": "hi"}})
            assert bad.status_code == 403
            assert orch.handled == []

            ok = client.post("/webhook/telegram/secbot",
                             headers={"X-Telegram-Bot-Api-Secret-Token": "topsecret"},
                             json={"update_id": 101, "message": {"from": {"id": 777},
                                   "chat": {"id": 777}, "text": "hi"}})
            assert ok.status_code == 200
            assert len(orch.handled) == 1
    finally:
        _remove_bot("secbot", 100, 101)


def test_non_allowlisted_user_not_handled(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [777])
    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", [])
    monkeypatch.setattr(settings, "webhook_secret", "")
    orch = _inject_bot("openbot", secret="")
    try:
        with TestClient(m.app) as client:
            resp = client.post("/webhook/telegram/openbot",
                               json={"update_id": 200, "message": {"from": {"id": 999},
                                     "chat": {"id": 999}, "text": "hi"}})
            assert resp.status_code == 200
            assert resp.json().get("skipped") == "not_allowed"
            assert orch.handled == []  # вне allowlist → Conversation/Lead не создаём, orchestrator не зовём
    finally:
        _remove_bot("openbot", 200)


def test_dedup_prevents_double_handle(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [777])
    monkeypatch.setattr(settings, "webhook_secret", "")
    monkeypatch.setattr("app.core.ai_reply.generate_and_send_reply", _fake_generate_and_send_reply)
    orch = _inject_bot("dedupbot", secret="")
    try:
        body = {"update_id": 555, "message": {"from": {"id": 777}, "chat": {"id": 777}, "text": "hi"}}
        with TestClient(m.app) as client:
            r1 = client.post("/webhook/telegram/dedupbot", json=body)
            r2 = client.post("/webhook/telegram/dedupbot", json=body)
        assert r1.status_code == 200 and r2.status_code == 200
        assert r2.json().get("dedup") is True
        assert len(orch.handled) == 1  # тот же update_id второй раз не обработан
    finally:
        _remove_bot("dedupbot", 555)


# ---- OFF-гейт: при выключенном боте нет авто-ответа (LLM/FAQ не вызываются) ----

def test_global_off_produces_no_reply():
    from app.core import flags
    from app.core.orchestrator import Orchestrator

    flags.reset()
    asyncio.run(flags.set_flag("bots_enabled", False))
    sent: list[str] = []

    class _Ch:
        channel = "telegram"

        async def send(self, chat_id, text, **kw):
            sent.append(text)
            return None

    orch = Orchestrator(channel=_Ch(), bot=BotConfig(id="offbot", scenario="admission"))
    msg = Message(channel="telegram", user_id="1", chat_id="1", text="сколько стоит?", kind="text")
    asyncio.run(orch.handle(msg))
    assert sent == []  # OFF → бот молчит: ни FAQ-автоответ, ни LLM
    flags.reset()

"""End-to-end тест вебхука Bitrix: form-событие → роутинг по BOT_ID → parse →
оркестратор → ответ в канал. Воронку подменяем FakeFunnel (детерминизм, без LLM).
"""
from fastapi.testclient import TestClient

import app.core.orchestrator as orch
import app.main as main
from app.channels.bitrix_openlines import BitrixOpenLinesAdapter
from app.config import BotConfig
from app.core.bots import BotRegistry
from app.core.orchestrator import Orchestrator


class _RecordingChannel(BitrixOpenLinesAdapter):
    """Реальный parse() из адаптера + перехват send() (без HTTP в портал)."""

    def __init__(self, bot: BotConfig) -> None:
        super().__init__(bot=bot)
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str, **kwargs) -> None:
        self.sent.append((chat_id, text))


def _wire_single_bot(monkeypatch) -> _RecordingChannel:
    """Подменить реестр и оркестраторы одним тестовым тур-ботом (BOT_ID=999)."""
    class FakeFunnel:
        async def handle(self, msg, state):
            return f"echo:{msg.text}"

    monkeypatch.setattr(orch, "get_funnel", lambda name: FakeFunnel())

    bot = BotConfig(id="t1", scenario="tours", bitrix_bot_id="999")
    channel = _RecordingChannel(bot)
    monkeypatch.setattr(main, "registry", BotRegistry([bot]))
    monkeypatch.setattr(main, "_bot_orchestrators", {bot.id: Orchestrator(channel=channel, bot=bot)})
    return channel


def test_webhook_form_event_routes_and_replies(monkeypatch):
    channel = _wire_single_bot(monkeypatch)
    client = TestClient(main.app)

    resp = client.post("/webhook/bitrix", data={
        "data[BOT][999][BOT_ID]": "999",
        "data[PARAMS][DIALOG_ID]": "chat1",
        "data[PARAMS][MESSAGE]": "привет",
        "data[PARAMS][FROM_USER_ID]": "u-e2e-1",
    })

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "bot": "t1"}
    assert channel.sent == [("chat1", "echo:привет")]


def test_webhook_unknown_bot_is_ignored(monkeypatch):
    channel = _wire_single_bot(monkeypatch)
    client = TestClient(main.app)

    resp = client.post("/webhook/bitrix", data={
        "data[BOT][555][BOT_ID]": "555",  # нет такого бота в реестре
        "data[PARAMS][DIALOG_ID]": "chat2",
        "data[PARAMS][MESSAGE]": "эй",
        "data[PARAMS][FROM_USER_ID]": "u-e2e-2",
    })

    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "reason": "unknown_bot"}
    assert channel.sent == []  # ничего не отправили


def test_webhook_accepts_json_body(monkeypatch):
    """JSON-форма события тоже принимается (ручная отладка)."""
    channel = _wire_single_bot(monkeypatch)
    client = TestClient(main.app)

    resp = client.post("/webhook/bitrix", json={
        "data": {
            "BOT": {"999": {"BOT_ID": "999"}},
            "PARAMS": {"DIALOG_ID": "chat3", "MESSAGE": "по json", "FROM_USER_ID": "u-e2e-3"},
        }
    })

    assert resp.json() == {"ok": True, "bot": "t1"}
    assert channel.sent == [("chat3", "echo:по json")]

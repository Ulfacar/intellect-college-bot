"""Тестовые Telegram-боты (песочница): маршрут /webhook/telegram/<id> и жёсткий сценарий."""
import asyncio

from app.config import BotConfig, TelegramBotConfig
from app.core.orchestrator import Orchestrator


def test_telegram_bot_config_parses_scenario():
    tb = TelegramBotConfig(id="college_tg", scenario="admission", token="222:BBB")
    assert tb.scenario == "admission"
    assert tb.token == "222:BBB"


def test_orchestrator_forces_scenario_from_telegram_bot():
    # Тест-бот фиксирует воронку так же, как WhatsApp-бот (через bot.scenario).
    bot = BotConfig(id="college_tg", scenario="admission")
    orch = Orchestrator(channel=None, bot=bot)
    assert orch.bot.scenario == "admission"
    assert orch._bot_id == "college_tg"


def test_per_bot_flag_enables_test_bot_without_waking_others():
    """Глобальный bots_enabled=OFF, но персональный флаг тест-бота=ON → этот бот отвечает,
    остальные (WhatsApp, без персонального флага) наследуют глобальный OFF и молчат."""
    from app.core import flags

    flags.reset()
    asyncio.run(flags.set_flag("bots_enabled", False))                  # глобально OFF (как на проде)
    asyncio.run(flags.set_flag("bots_enabled:college_tg", True))   # включаем точечно тест-бот

    admission_bot = Orchestrator(channel=None, bot=BotConfig(id="college_tg", scenario="admission"))
    whatsapp_bot = Orchestrator(channel=None, bot=BotConfig(id="wa_main", scenario="admission"))
    dev_bot = Orchestrator(channel=None)  # без bot → только глобальный флаг

    assert asyncio.run(admission_bot._bots_on()) is True       # персональный флаг переопределил
    assert asyncio.run(whatsapp_bot._bots_on()) is False   # наследует глобальный OFF — молчит
    assert asyncio.run(dev_bot._bots_on()) is False        # тоже по глобальному

    flags.reset()


def test_unknown_telegram_bot_returns_404():
    from fastapi.testclient import TestClient

    import app.main as m

    # В тестовой среде telegram_bots не настроены → любой bot_id неизвестен → 404.
    with TestClient(m.app) as client:
        resp = client.post("/webhook/telegram/nope", json={"message": {"text": "hi"}})
    assert resp.status_code == 404
    assert resp.json()["reason"] == "unknown_bot"


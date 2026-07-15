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


def test_global_off_disables_all_bots_even_with_individual_on():
    """AND-формула (решение владельца #3): общий bots_enabled=OFF выключает ВСЕ боты,
    даже если у бота персональный флаг ON. Отменяет прежнюю override-семантику."""
    from app.core import flags

    flags.reset()
    asyncio.run(flags.set_flag("bots_enabled", False))                # глобально OFF
    asyncio.run(flags.set_flag("bots_enabled:college_tg", True))      # индивидуальный ON

    admission_bot = Orchestrator(channel=None, bot=BotConfig(id="college_tg", scenario="admission"))
    whatsapp_bot = Orchestrator(channel=None, bot=BotConfig(id="wa_main", scenario="admission"))
    dev_bot = Orchestrator(channel=None)  # без bot → только глобальный флаг

    # global OFF имеет абсолютный приоритет: индивидуальный ON его не обходит.
    assert asyncio.run(admission_bot._bots_on()) is False
    assert asyncio.run(whatsapp_bot._bots_on()) is False
    assert asyncio.run(dev_bot._bots_on()) is False

    flags.reset()


def test_global_on_individual_off_disables_only_selected():
    """При общем ON индивидуальный OFF выключает только выбранного бота, остальные работают."""
    from app.core import flags

    flags.reset()
    asyncio.run(flags.set_flag("bots_enabled", True))                 # глобально ON
    asyncio.run(flags.set_flag("bots_enabled:college_tg", False))     # индивидуально OFF

    off_bot = Orchestrator(channel=None, bot=BotConfig(id="college_tg", scenario="admission"))
    on_bot = Orchestrator(channel=None, bot=BotConfig(id="college_other", scenario="admission"))
    dev_bot = Orchestrator(channel=None)

    assert asyncio.run(off_bot._bots_on()) is False   # global ON AND individual OFF
    assert asyncio.run(on_bot._bots_on()) is True     # global ON AND default individual ON
    assert asyncio.run(dev_bot._bots_on()) is True    # global ON, без bot_id

    flags.reset()


def test_unknown_telegram_bot_returns_404():
    from fastapi.testclient import TestClient

    import app.main as m

    # В тестовой среде telegram_bots не настроены → любой bot_id неизвестен → 404.
    with TestClient(m.app) as client:
        resp = client.post("/webhook/telegram/nope", json={"message": {"text": "hi"}})
    assert resp.status_code == 404
    assert resp.json()["reason"] == "unknown_bot"


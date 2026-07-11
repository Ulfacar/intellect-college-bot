"""Фаза 1: реестр ботов, маршрутизация по BOT_ID, bot-aware выбор воронки."""
import asyncio

from app.channels.base import Message
from app.channels.bitrix_openlines import bot_id_from_event
from app.config import BotConfig
from app.core.bots import BotRegistry
from app.core.orchestrator import Orchestrator
from app.core.state import state_store


def _registry() -> BotRegistry:
    return BotRegistry([
        BotConfig(id="frunze_tours_1", scenario="tours", title="FrunzeTravel",
                  bitrix_bot_id="101", bitrix_line_id="1", category_id="2"),
        BotConfig(id="frunze_tours_2", scenario="tours", title="FrunzeTravel2",
                  bitrix_bot_id="102", bitrix_line_id="2", category_id="2"),
        BotConfig(id="getvisa", scenario="visa", title="GetVisa",
                  bitrix_bot_id="103", bitrix_line_id="3", category_id="4"),
    ])


def test_registry_lookup_by_id_and_bot_id():
    reg = _registry()
    assert reg.by_id("getvisa").scenario == "visa"
    assert reg.by_bitrix_bot_id("101").id == "frunze_tours_1"
    assert reg.by_bitrix_bot_id(102).id == "frunze_tours_2"  # int → str
    assert reg.by_line("3").id == "getvisa"
    assert reg.by_bitrix_bot_id("999") is None
    assert len(reg.all()) == 3


def test_default_bots_match_starting_wappi_profiles():
    from app.config import DEFAULT_BOTS

    reg = BotRegistry(DEFAULT_BOTS)

    assert [bot.id for bot in reg.all()] == ["frunze_tours", "frunze_tours_sezim", "getvisa"]
    assert reg.by_wappi_profile_id("00000000-0000").id == "frunze_tours"
    assert reg.by_wappi_profile_id("6a74fb33-16aa").id == "frunze_tours_sezim"
    assert reg.by_wappi_profile_id("00000000-0000").id == "getvisa"
    assert reg.by_id("frunze_tours").manager_name == "Адеми"
    assert reg.by_id("frunze_tours_sezim").manager_name == "Сезим"


def test_registry_ignores_unconfigured_bots():
    """Боты без BOT_ID (до Фазы 0) не участвуют в маршрутизации, но видимы по id."""
    reg = BotRegistry([BotConfig(id="getvisa", scenario="visa")])
    assert reg.by_id("getvisa") is not None
    assert reg.by_bitrix_bot_id("") is None


def test_bot_id_from_event_simple_and_bitrix_shapes():
    assert bot_id_from_event({"bot_id": 101}) == "101"
    assert bot_id_from_event({"data": {"BOT": {"55": {"BOT_ID": "55"}}}}) == "55"
    assert bot_id_from_event({"data": {"BOT": [{"BOT_ID": 77}]}}) == "77"
    assert bot_id_from_event({"data": {}}) is None


def test_tours_bot_forces_funnel_without_keywords(monkeypatch):
    """Тур-бот ставит воронку tours по сценарию, даже если в тексте нет тур-слов."""
    import app.core.orchestrator as orch

    seen = {}

    class FakeFunnel:
        async def handle(self, msg, state):
            seen["funnel"] = state.funnel
            return "ок"

    monkeypatch.setattr(orch, "get_funnel", lambda name: FakeFunnel())

    class FakeChannel:
        channel = "bitrix_openlines"

        async def send(self, chat_id, text, **kwargs):
            ...

    bot = BotConfig(id="frunze_tours_1", scenario="tours")
    state = asyncio.run(state_store.load("bot-user-tours"))
    state.funnel = None
    msg = Message(channel="bitrix_openlines", user_id="bot-user-tours", chat_id="9",
                  text="здравствуйте")  # без ключевых слов про тур
    asyncio.run(Orchestrator(channel=FakeChannel(), bot=bot).handle(msg))

    assert seen["funnel"] == "tours"


def test_visa_bot_forces_visa_funnel(monkeypatch):
    import app.core.orchestrator as orch

    seen = {}

    class FakeFunnel:
        async def handle(self, msg, state):
            seen["funnel"] = state.funnel
            return None

    monkeypatch.setattr(orch, "get_funnel", lambda name: FakeFunnel())

    class FakeChannel:
        channel = "bitrix_openlines"

        async def send(self, chat_id, text, **kwargs):
            ...

    bot = BotConfig(id="getvisa", scenario="visa")
    state = asyncio.run(state_store.load("bot-user-visa"))
    state.funnel = None
    msg = Message(channel="bitrix_openlines", user_id="bot-user-visa", chat_id="9",
                  text="добрый день")
    asyncio.run(Orchestrator(channel=FakeChannel(), bot=bot).handle(msg))

    assert seen["funnel"] == "visa"

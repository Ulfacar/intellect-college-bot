import asyncio

from app.channels.base import Message
from app.channels.bitrix_openlines import bot_id_from_event
from app.config import BotConfig
from app.core.bots import BotRegistry
from app.core.orchestrator import Orchestrator
from app.core.state import state_store


def _registry() -> BotRegistry:
    return BotRegistry([
        BotConfig(id="college_1", scenario="admission", title="Intellect 1", bitrix_bot_id="101", bitrix_line_id="1"),
        BotConfig(id="college_2", scenario="admission", title="Intellect 2", bitrix_bot_id="102", bitrix_line_id="2"),
        BotConfig(id="college_3", scenario="admission", title="Intellect 3", bitrix_bot_id="103", bitrix_line_id="3"),
    ])


def test_registry_lookup_by_id_and_bot_id():
    reg = _registry()
    assert reg.by_id("college_1").scenario == "admission"
    assert reg.by_bitrix_bot_id("101").id == "college_1"
    assert reg.by_bitrix_bot_id(102).id == "college_2"
    assert reg.by_line("3").id == "college_3"
    assert reg.by_bitrix_bot_id("999") is None
    assert len(reg.all()) == 3


def test_default_bots_are_college_placeholders():
    from app.config import DEFAULT_BOTS

    reg = BotRegistry(DEFAULT_BOTS)
    assert [bot.id for bot in reg.all()] == ["college_1", "college_2", "college_3"]
    assert all(bot.scenario == "admission" for bot in reg.all())
    assert all(not bot.wappi_profile_id for bot in reg.all())


def test_registry_ignores_unconfigured_bots():
    reg = BotRegistry([BotConfig(id="college_1", scenario="admission")])
    assert reg.by_id("college_1") is not None
    assert reg.by_bitrix_bot_id("") is None


def test_bot_id_from_event_simple_and_bitrix_shapes():
    assert bot_id_from_event({"bot_id": 101}) == "101"
    assert bot_id_from_event({"data": {"BOT": {"55": {"BOT_ID": "55"}}}}) == "55"
    assert bot_id_from_event({"data": {"BOT": [{"BOT_ID": 77}]}}) == "77"
    assert bot_id_from_event({"data": {}}) is None


def test_college_bot_forces_admission_funnel(monkeypatch):
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

    bot = BotConfig(id="college_1", scenario="admission")
    state = asyncio.run(state_store.load("bot-user-admission"))
    state.funnel = None
    msg = Message(channel="bitrix_openlines", user_id="bot-user-admission", chat_id="9", text="здравствуйте")
    asyncio.run(Orchestrator(channel=FakeChannel(), bot=bot).handle(msg))

    assert seen["funnel"] == "admission"


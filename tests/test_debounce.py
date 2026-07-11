"""Дебаунс входящих: быстрые реплики клиента склеиваются в один ход LLM.

При DEBOUNCE_SECONDS>0 несколько коротких сообщений подряд («Хочу в поступлениецию» / «2 взрослых» /
«конец июля») должны собраться в один ход и дать ОДИН связный ответ (без задвоений), при этом
каждое входящее логируется в панель сразу. При 0 — синхронное поведение как раньше.
"""
import asyncio

from app.channels.base import Message
from app.config import settings


class FakeChannel:
    channel = "telegram"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def parse(self, raw):  # pragma: no cover
        ...

    async def send(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class RecordingFunnel:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def handle(self, msg, state):
        self.calls.append(msg.text)
        return f"ответ на: {msg.text}"


def _fresh_stores() -> None:
    """Изолировать in-memory синглтоны стора/состояния между тестами."""
    from app.core.state import state_store
    from app.integrations.panel.store import _memory_store
    state_store._store.clear()
    _memory_store._conv.clear()
    _memory_store._mid = 0


def test_debounce_coalesces_rapid_messages(monkeypatch):
    import app.core.orchestrator as orch
    from app.core import flags
    from app.core.orchestrator import Orchestrator
    from app.core.state import state_store
    from app.integrations.panel.store import get_conversation_store

    flags.reset()
    _fresh_stores()
    monkeypatch.setattr(settings, "debounce_seconds", 0.05)
    funnel = RecordingFunnel()
    monkeypatch.setattr(orch, "get_funnel", lambda name: funnel)

    state = asyncio.run(state_store.load("deb-user"))
    state.funnel = "admission"

    ch = FakeChannel()
    orchestrator = Orchestrator(channel=ch)

    async def fire():
        for t in ("Хочу в поступлениецию", "2 взрослых", "конец июля"):
            await orchestrator.handle(
                Message(channel="telegram", user_id="deb-user", chat_id="1", text=t))
        await asyncio.sleep(0.2)  # дать тихому окну истечь и флашу отработать

    asyncio.run(fire())

    # Воронка вызвана РОВНО один раз — со склеенным текстом всех трёх реплик.
    assert funnel.calls == ["Хочу в поступлениецию\n2 взрослых\nконец июля"]
    # Отправлен ОДИН ответ (без задвоений).
    assert ch.sent == [("1", "ответ на: Хочу в поступлениецию\n2 взрослых\nконец июля")]
    # Все три входящих залогированы в панель сразу.
    conv = asyncio.run(get_conversation_store().get("deb-user"))
    client_msgs = [m.text for m in conv.messages if m.sender == "client"]
    assert client_msgs == ["Хочу в поступлениецию", "2 взрослых", "конец июля"]


def test_debounce_off_is_inline(monkeypatch):
    """При debounce_seconds=0 одно сообщение → немедленный ответ (как раньше)."""
    import app.core.orchestrator as orch
    from app.core import flags
    from app.core.orchestrator import Orchestrator
    from app.core.state import state_store

    flags.reset()
    _fresh_stores()
    monkeypatch.setattr(settings, "debounce_seconds", 0.0)
    funnel = RecordingFunnel()
    monkeypatch.setattr(orch, "get_funnel", lambda name: funnel)

    state = asyncio.run(state_store.load("inl-user"))
    state.funnel = "admission"

    ch = FakeChannel()
    orchestrator = Orchestrator(channel=ch)
    asyncio.run(orchestrator.handle(
        Message(channel="telegram", user_id="inl-user", chat_id="9", text="привет")))

    assert funnel.calls == ["привет"]
    assert ch.sent == [("9", "ответ на: привет")]


"""Нативный перехват: когда менеджер взял диалог (state.intercepted=True), бот молчит."""
import asyncio
from unittest.mock import AsyncMock

from app.agent import runner
from app.core.state import DialogState


def test_runner_silent_when_intercepted(monkeypatch):
    """run_admission_turn не зовёт Claude и ничего не отвечает при перехвате."""
    state = DialogState(user_id="u1", funnel="admission")
    state.intercepted = True
    fake = AsyncMock()
    fake.messages.create = AsyncMock()
    monkeypatch.setattr(runner, "client", lambda: fake)

    reply = asyncio.run(runner.run_admission_turn(state, "привет"))

    assert reply is None
    fake.messages.create.assert_not_called()
    assert state.history == []  # история не тронута


def test_orchestrator_silent_when_intercepted(monkeypatch):
    """Оркестратор не отвечает ни в одной воронке, пока менеджер ведёт диалог."""
    from app.channels.base import Message
    from app.core.orchestrator import Orchestrator
    from app.core.state import state_store

    sent = []

    class FakeChannel:
        channel = "telegram"

        async def parse(self, raw):  # pragma: no cover
            ...

        async def send(self, chat_id, text, **kwargs):
            sent.append((chat_id, text))

    state = asyncio.run(state_store.load("intercepted-user"))
    state.funnel = "admission"
    state.intercepted = True

    msg = Message(channel="telegram", user_id="intercepted-user", chat_id="42", text="есть поступление?")
    asyncio.run(Orchestrator(channel=FakeChannel()).handle(msg))

    assert sent == []  # бот промолчал


def test_handoff_to_manager_auto_intercepts(monkeypatch):
    """Когда воронка переводит стадию в manager, бот шлёт прощальную реплику и
    автоматически глушится. Пока менеджер не подключился, первое сообщение клиента
    получает разовое подтверждение ожидания, дальше бот молчит (анти-спам)."""
    import app.core.orchestrator as orch
    from app.channels.base import Message
    from app.core.orchestrator import Orchestrator
    from app.core.state import state_store

    sent = []

    class FakeChannel:
        channel = "telegram"

        async def parse(self, raw):  # pragma: no cover
            ...

        async def send(self, chat_id, text, **kwargs):
            sent.append((chat_id, text))

    class HandoffFunnel:
        async def handle(self, msg, state):
            state.stage = "manager"
            return "Передаю менеджеру 🙏"

    monkeypatch.setattr(orch, "get_funnel", lambda name: HandoffFunnel())

    state = asyncio.run(state_store.load("handoff-user"))
    state.funnel = "admission"
    state.intercepted = False

    orchestrator = Orchestrator(channel=FakeChannel())
    msg = Message(channel="telegram", user_id="handoff-user", chat_id="77", text="хочу к менеджеру")
    asyncio.run(orchestrator.handle(msg))

    # Прощальная реплика этого хода ушла...
    assert sent == [("77", "Передаю менеджеру 🙏")]
    # ...и бот теперь заглушен.
    saved = asyncio.run(state_store.load("handoff-user"))
    assert saved.intercepted is True

    # Следующее сообщение клиента — бот один раз подтверждает ожидание (не тишина).
    from app.core.branding import wait_ack_for
    sent.clear()
    msg2 = Message(channel="telegram", user_id="handoff-user", chat_id="77", text="ещё вопрос")
    asyncio.run(orchestrator.handle(msg2))
    assert sent == [("77", wait_ack_for("admission"))]

    # А вот уже следующее — молчит (ack разовый, дальше напомнит awaiting-джоба менеджеру).
    sent.clear()
    msg3 = Message(channel="telegram", user_id="handoff-user", chat_id="77", text="ну где же вы")
    asyncio.run(orchestrator.handle(msg3))
    assert sent == []


def test_handoff_no_ack_when_manager_engaged(monkeypatch):
    """Если менеджер уже закрепил диалог (assigned_to) — бот молчит даже на stage=manager,
    чтобы не отвечать поверх живого человека."""
    import app.core.orchestrator as orch
    from app.channels.base import Message
    from app.core.orchestrator import Orchestrator
    from app.core.state import state_store
    from app.integrations.panel.store import get_conversation_store

    sent = []

    class FakeChannel:
        channel = "telegram"

        async def parse(self, raw):  # pragma: no cover
            ...

        async def send(self, chat_id, text, **kwargs):
            sent.append((chat_id, text))

    monkeypatch.setattr(orch, "get_funnel", lambda name: None)  # не должен вызываться

    state = asyncio.run(state_store.load("claimed-user"))
    state.funnel = "admission"
    state.stage = "manager"
    state.intercepted = True

    # менеджер закрепил диалог за собой
    asyncio.run(get_conversation_store().claim("claimed-user", "adema"))

    msg = Message(channel="telegram", user_id="claimed-user", chat_id="9", text="алло?")
    asyncio.run(Orchestrator(channel=FakeChannel()).handle(msg))

    assert sent == []  # бот не вмешивается — отвечает менеджер


def test_concurrent_messages_same_dialog_serialized(monkeypatch):
    """Два быстрых сообщения одного клиента (два параллельных вебхука) обрабатываются
    строго по очереди — без гонки истории и без задвоения ответов."""
    import app.core.orchestrator as orch
    from app.channels.base import Message
    from app.core.orchestrator import Orchestrator
    from app.core.state import state_store
    from app.core import flags

    flags.reset()
    events = []
    sent = []

    class SlowFunnel:
        async def handle(self, msg, state):
            events.append(("start", msg.text))
            await asyncio.sleep(0.01)        # окно, в которое влезла бы гонка без лока
            events.append(("end", msg.text))
            return f"ответ {msg.text}"

    monkeypatch.setattr(orch, "get_funnel", lambda name: SlowFunnel())

    class FakeChannel:
        channel = "telegram"

        async def parse(self, raw):  # pragma: no cover
            ...

        async def send(self, chat_id, text, **kw):
            sent.append(text)

    state = asyncio.run(state_store.load("race-user"))
    state.funnel = "admission"
    state.intercepted = False

    orchestrator = Orchestrator(channel=FakeChannel())

    async def fire():
        m1 = Message(channel="telegram", user_id="race-user", chat_id="1", text="A")
        m2 = Message(channel="telegram", user_id="race-user", chat_id="1", text="B")
        await asyncio.gather(orchestrator.handle(m1), orchestrator.handle(m2))

    asyncio.run(fire())

    # Каждый ход завершился до начала следующего (лок сериализовал), порядок любой.
    assert events in (
        [("start", "A"), ("end", "A"), ("start", "B"), ("end", "B")],
        [("start", "B"), ("end", "B"), ("start", "A"), ("end", "A")],
    )
    assert len(sent) == 2          # оба ответа, без дублей/потерь


def test_reply_dropped_when_intercepted_mid_flight(monkeypatch):
    """Менеджер перехватил, пока генерировался ответ → бот НЕ отправляет этот ответ."""
    import app.core.orchestrator as orch
    from app.channels.base import Message
    from app.core.orchestrator import Orchestrator
    from app.core.state import state_store

    sent = []

    class FakeChannel:
        channel = "telegram"

        async def parse(self, raw):  # pragma: no cover
            ...

        async def send(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    class MidFlightInterceptFunnel:
        async def handle(self, msg, state):
            # имитируем: менеджер нажал «Перехватить» во время генерации ответа
            state.intercepted = True
            return "ответ, который не должен уйти"

    monkeypatch.setattr(orch, "get_funnel", lambda name: MidFlightInterceptFunnel())

    state = asyncio.run(state_store.load("midflight-user"))
    state.funnel = "admission"
    state.intercepted = False

    msg = Message(channel="telegram", user_id="midflight-user", chat_id="55", text="вопрос")
    asyncio.run(Orchestrator(channel=FakeChannel()).handle(msg))

    assert sent == []  # ответ дропнут — отвечает менеджер


"""Тесты админ-панели: лог диалогов (store), логирование оркестратором,
перехват (бот замолкает), эндпоинты доски + авторизация.
"""
import asyncio

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.main as main
from app.channels.base import Message
from app.config import BotConfig
from app.core.orchestrator import Orchestrator
from app.integrations.crm.db import init_models
from app.integrations.panel import store as panel_store
from app.integrations.panel.store import PostgresConversationStore


def _clear_memory():
    """Очистить процесс-глобальные in-memory стораджи между тестами."""
    panel_store._memory_store._conv.clear()
    panel_store._memory_store._audit.clear()
    from app.core.state import state_store
    state_store._store.clear()
    from app.admin import ratelimit
    ratelimit.reset()
    from app.core import flags
    flags.reset()


class _FakeChannel:
    channel = "whatsapp"

    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


def _msg(user_id, text):
    return Message(channel="whatsapp", user_id=user_id, chat_id=user_id, text=text)


def _auth_client():
    """TestClient с залогиненным менеджером (дефолт admin/frunze) — cookie-сессия.
    base_url=https — cookie сессии Secure (https_only), иначе по http не сохранится."""
    client = TestClient(main.app, base_url="https://testserver")
    r = client.post("/admin/login", data={"login": "admin", "password": "frunze"})
    assert r.status_code == 200  # редирект на /admin отрабатывает, сессия установлена
    return client


# ---------------- store (Postgres на SQLite) ----------------
def test_postgres_conversation_store_round_trip():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite://",
                                     connect_args={"check_same_thread": False}, poolclass=StaticPool)
        await init_models(engine)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        store = PostgresConversationStore(sessionmaker=sm)

        await store.add_message("996700111", "client", "виза в США", channel="whatsapp", bot_id="getvisa")
        await store.update_meta("996700111", funnel="visa", stage="qualification",
                                qualification={"name": "Саодат"})
        await store.add_message("996700111", "bot", "Как могу к вам обращаться?")

        cards = await store.list_cards("visa")
        assert len(cards) == 1
        assert cards[0].user_id == "996700111"
        assert cards[0].last_text == "Как могу к вам обращаться?"
        assert cards[0].qualification["name"] == "Саодат"

        conv = await store.get("996700111")
        assert [m.sender for m in conv.messages] == ["client", "bot"]
        assert conv.last_sender == "bot"  # последним писал бот
        await engine.dispose()

    asyncio.run(scenario())


def test_archived_conversation_hidden_from_lists():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite://",
                                     connect_args={"check_same_thread": False}, poolclass=StaticPool)
        await init_models(engine)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        store = PostgresConversationStore(sessionmaker=sm)

        await store.add_message("u-active", "client", "нужна виза", channel="whatsapp")
        await store.update_meta("u-active", funnel="visa")
        await store.add_message("u-archived", "client", "https://instagram.com/ad", channel="whatsapp")
        await store.update_meta("u-archived", funnel="visa")
        await store.set_archived("u-archived", True)

        assert [c.user_id for c in await store.list_cards("visa")] == ["u-active"]
        assert [c.user_id for c in await store.all_conversations()] == ["u-active"]

        await store.set_archived("u-archived", False)
        assert {c.user_id for c in await store.list_cards("visa")} == {"u-active", "u-archived"}
        await engine.dispose()

    asyncio.run(scenario())


def test_postgres_bulk_archive_hides_from_lists():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite://",
                                     connect_args={"check_same_thread": False}, poolclass=StaticPool)
        await init_models(engine)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        store = PostgresConversationStore(sessionmaker=sm)

        for uid in ("u-active", "u-bulk-1", "u-bulk-2"):
            await store.add_message(uid, "client", "hello", channel="whatsapp")
            await store.update_meta(uid, funnel="visa")

        changed = await store.set_archived_many(["u-bulk-1", "u-bulk-2"], True)

        assert changed == 2
        assert [c.user_id for c in await store.list_cards("visa")] == ["u-active"]
        assert [c.user_id for c in await store.all_conversations()] == ["u-active"]
        await engine.dispose()

    asyncio.run(scenario())


def test_postgres_add_message_unarchives_existing_dialog():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite://",
                                     connect_args={"check_same_thread": False}, poolclass=StaticPool)
        await init_models(engine)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        store = PostgresConversationStore(sessionmaker=sm)

        await store.add_message("u-return", "client", "old", channel="whatsapp")
        await store.update_meta("u-return", funnel="tours")
        await store.set_archived("u-return", True)

        await store.add_message("u-return", "client", "new", channel="whatsapp")

        conv = await store.get("u-return")
        assert conv.archived is False
        assert [c.user_id for c in await store.list_cards("tours")] == ["u-return"]
        await engine.dispose()

    asyncio.run(scenario())


def test_memory_archived_conversation_hidden_from_lists():
    async def scenario():
        store = panel_store.MemoryConversationStore()
        await store.add_message("u-active", "client", "нужна виза", channel="whatsapp")
        await store.update_meta("u-active", funnel="visa")
        await store.add_message("u-archived", "client", "https://instagram.com/ad", channel="whatsapp")
        await store.update_meta("u-archived", funnel="visa")
        await store.set_archived("u-archived", True)

        assert [c.user_id for c in await store.list_cards("visa")] == ["u-active"]
        assert [c.user_id for c in await store.all_conversations()] == ["u-active"]

    asyncio.run(scenario())


def test_memory_add_message_unarchives_existing_dialog():
    async def scenario():
        store = panel_store.MemoryConversationStore()
        await store.add_message("u-return", "client", "old", channel="whatsapp")
        await store.update_meta("u-return", funnel="tours")
        await store.set_archived("u-return", True)

        await store.add_message("u-return", "client", "new", channel="whatsapp")

        conv = await store.get("u-return")
        assert conv.archived is False
        assert [c.user_id for c in await store.list_cards("tours")] == ["u-return"]

    asyncio.run(scenario())


def test_memory_bulk_archive_hides_from_lists():
    async def scenario():
        store = panel_store.MemoryConversationStore()
        for uid in ("u-active", "u-bulk-1", "u-bulk-2"):
            await store.add_message(uid, "client", "hello", channel="whatsapp")
            await store.update_meta(uid, funnel="visa")

        changed = await store.set_archived_many(["u-bulk-1", "u-bulk-2"], True)

        assert changed == 2
        assert [c.user_id for c in await store.list_cards("visa")] == ["u-active"]
        assert [c.user_id for c in await store.all_conversations()] == ["u-active"]

    asyncio.run(scenario())


def test_card_model_flags_client_waiting():
    """Карточка с последней репликой клиента → сигнал ожидания (waiting)."""
    from datetime import datetime, timedelta, timezone
    from app.admin.router import _card_model
    from app.integrations.panel.store import ConversationView

    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    conv = ConversationView(user_id="996700777", funnel="visa", stage="qualification",
                            qualification={"name": "Айгуль"}, last_sender="client",
                            last_text="а виза за сколько дней?",
                            last_message_at=now - timedelta(minutes=25))
    m = _card_model(conv, now)
    assert m["initials"] == "АЙ"
    assert m["wait_level"] == "hot"       # ждёт 25 мин (> 20)
    assert "мин" in m["wait_label"]
    assert m["last_sender"] == "client"


def test_card_model_marks_link_only_greeting_as_noise():
    from datetime import datetime, timezone
    from app.admin.router import _card_model
    from app.integrations.panel.store import ConversationView

    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    noise = ConversationView(user_id="spam", funnel="tours", stage="greeting",
                             last_sender="client", last_text="https://instagram.com/promo",
                             last_message_at=now)
    qualified = ConversationView(user_id="real", funnel="tours", stage="greeting",
                                 last_sender="client", last_text="https://instagram.com/profile",
                                 qualification={"name": "Айгуль"},
                                 last_message_at=now)

    assert _card_model(noise, now)["is_noise"] is True
    assert _card_model(qualified, now)["is_noise"] is False


def test_board_maps_follow_up_stage():
    """Follow-up is its own column: these leads need a repeat touch, not a fresh greeting."""
    from datetime import datetime, timezone
    from app.admin.router import _build_board
    from app.integrations.panel.store import ConversationView

    conv = ConversationView(user_id="996700888", funnel="visa", stage="follow_up")
    columns, _ = _build_board([conv], datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc))

    follow_up = next(c for c in columns if c["key"] == "follow_up")
    assert follow_up["cards"][0]["user_id"] == "996700888"


def test_board_routes_silent_leads_to_computed_column():
    """Старый застрявший лид вынимается из обычной стадии в колонку «Молчат»."""
    from datetime import datetime, timedelta, timezone
    from app.admin.router import COLUMN_TO_STAGE, _build_board
    from app.integrations.panel.store import ConversationView

    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    conv = ConversationView(
        user_id="silent-1",
        funnel="tours",
        stage="qualification",
        channel="whatsapp",
        bot_id="frunze_tours",
        chat_id="silent-1@c.us",
        last_sender="client",
        last_text="а что по туру?",
        last_message_at=now - timedelta(hours=30),
    )

    columns, metrics = _build_board([conv], now)

    silent = next(c for c in columns if c["key"] == "silent")
    qualification = next(c for c in columns if c["key"] == "qualification")
    assert silent["cards"][0]["user_id"] == "silent-1"
    assert qualification["cards"] == []
    assert metrics["silent"] == 1
    assert "silent" not in COLUMN_TO_STAGE


# ---------------- логирование оркестратором ----------------
def test_orchestrator_logs_client_and_bot(monkeypatch):
    _clear_memory()
    monkeypatch.setattr("app.agent.llm.settings.openrouter_api_key", "")
    ch = _FakeChannel()
    bot = BotConfig(id="frunze_tours_1", scenario="tours")
    asyncio.run(Orchestrator(channel=ch, bot=bot).handle(_msg("u-log-1", "хочу тур")))

    # Диалог хранится по композитному ключу bot_id:номер; сам номер — в phone.
    conv = asyncio.run(panel_store.get_conversation_store().get("frunze_tours_1:u-log-1"))
    assert conv is not None
    assert conv.phone == "u-log-1"
    assert conv.funnel == "tours"
    assert [m.sender for m in conv.messages] == ["client", "bot"]
    assert conv.messages[0].text == "хочу тур"
    assert "Лид на тур" in conv.ai_summary
    assert conv.manager_next_step
    assert conv.lead_temperature in {"new", "warm", "hot"}
    assert ch.sent  # бот ответил


# ---------------- перехват глушит бота ----------------
def test_takeover_mutes_bot_but_logs_client(monkeypatch):
    _clear_memory()
    monkeypatch.setattr("app.agent.llm.settings.openrouter_api_key", "")
    ch = _FakeChannel()
    orch = Orchestrator(channel=ch, bot=BotConfig(id="frunze_tours_1", scenario="tours"))

    key = "frunze_tours_1:u-int-1"  # ключ диалога = bot_id:номер
    asyncio.run(orch.handle(_msg("u-int-1", "здравствуйте")))
    assert len(ch.sent) == 1  # бот ответил на первое

    # Менеджер перехватывает (по ключу диалога).
    from app.admin.router import _set_intercept
    asyncio.run(_set_intercept(key, True))

    asyncio.run(orch.handle(_msg("u-int-1", "второе сообщение")))
    assert len(ch.sent) == 1  # бот молчит — нового ответа нет

    conv = asyncio.run(panel_store.get_conversation_store().get(key))
    # Но входящее клиента залогировано (менеджер должен видеть).
    assert conv.messages[-1].text == "второе сообщение"
    assert conv.messages[-1].sender == "client"
    assert conv.intercepted is True


# ---------------- эндпоинты доски + авторизация ----------------
def test_board_requires_auth():
    client = TestClient(main.app)
    assert client.get("/admin/board/visa").status_code == 401


def test_board_renders_card_with_auth(monkeypatch):
    _clear_memory()
    store = panel_store.get_conversation_store()
    asyncio.run(store.add_message("996700222", "client", "виза в Канаду", channel="whatsapp"))
    asyncio.run(store.update_meta("996700222", funnel="visa", stage="qualification",
                                  qualification={"name": "Адам"}))

    client = _auth_client()
    resp = client.get("/admin/board/visa")
    assert resp.status_code == 200
    assert "996700222" in resp.text
    assert "Адам" in resp.text
    assert "Квалификация" in resp.text  # колонка канбана


def test_archive_endpoint_hides_card_and_audits():
    _clear_memory()
    store = panel_store.get_conversation_store()
    asyncio.run(store.add_message("u-archive-1", "client", "https://instagram.com/ad", channel="whatsapp"))
    asyncio.run(store.update_meta("u-archive-1", funnel="visa", stage="greeting"))

    client = _auth_client()
    assert "u-archive-1" in client.get("/admin/board/visa").text
    resp = client.post("/admin/conversation/u-archive-1/archive")
    assert resp.status_code == 200 and resp.json()["ok"] is True

    assert "u-archive-1" not in client.get("/admin/board/visa").text
    assert "u-archive-1" not in client.get("/admin/search", params={"q": "archive"}).text
    assert any(a["action"] == "archive" and a["user_id"] == "u-archive-1"
               for a in panel_store._memory_store._audit)


# ---------------- ответ менеджера из панели (двусторонняя отправка) ----------------
def test_bulk_archive_endpoint_hides_cards_and_audits():
    _clear_memory()
    store = panel_store.get_conversation_store()
    for uid in ("u-live", "u-bulk-a", "u-bulk-b"):
        asyncio.run(store.add_message(uid, "client", "archive me", channel="whatsapp"))
        asyncio.run(store.update_meta(uid, funnel="visa", stage="qualification"))

    client = _auth_client()
    resp = client.post("/admin/conversations/archive",
                       data={"user_ids_csv": "u-bulk-a,u-bulk-b"})

    assert resp.status_code == 200
    assert 'data-user-id="u-bulk-a"' not in resp.text and 'data-user-id="u-bulk-b"' not in resp.text
    board = client.get("/admin/board/visa").text
    assert "u-live" in board
    assert "u-bulk-a" not in board
    assert any(a["action"] == "archive_many" and a["detail"] == "count=2"
               for a in panel_store._memory_store._audit)


def test_archive_noise_endpoint_uses_card_noise_logic_and_audits():
    _clear_memory()
    store = panel_store.get_conversation_store()
    asyncio.run(store.add_message("noise-1", "client", "https://instagram.com/ad", channel="whatsapp"))
    asyncio.run(store.update_meta("noise-1", funnel="tours", stage="greeting"))
    asyncio.run(store.add_message("noise-2", "client", "https://t.me/ad", channel="whatsapp"))
    asyncio.run(store.update_meta("noise-2", funnel="visa", stage="greeting"))
    asyncio.run(store.add_message("real-1", "client", "https://instagram.com/ad", channel="whatsapp"))
    asyncio.run(store.update_meta("real-1", funnel="visa", stage="qualification",
                                  qualification={"name": "Lead"}))

    client = _auth_client()
    resp = client.post("/admin/conversations/archive-noise")

    assert resp.status_code == 200
    assert 'data-user-id="noise-1"' not in client.get("/admin/search", params={"q": "noise-1"}).text
    assert 'data-user-id="noise-2"' not in client.get("/admin/search", params={"q": "noise-2"}).text
    assert 'data-user-id="real-1"' in client.get("/admin/search", params={"q": "real-1"}).text
    assert any(a["action"] == "archive_noise" and a["detail"] == "count=2"
               for a in panel_store._memory_store._audit)


def test_manager_send_replies_and_takes_over(monkeypatch):
    _clear_memory()
    store = panel_store.get_conversation_store()
    asyncio.run(store.add_message("996700333", "client", "здравствуйте", channel="whatsapp",
                                  bot_id="getvisa", chat_id="996700333@c.us"))

    sent = []
    async def fake_send(channel, bot_id, chat_id, text):
        sent.append((channel, bot_id, chat_id, text))
        return "wappi-msg-1"
    monkeypatch.setattr("app.channels.outbound.send_to_client", fake_send)

    client = _auth_client()
    resp = client.post("/admin/conversation/996700333/send",
                       data={"text": "Это менеджер Медина, помогу вам"})
    assert resp.status_code == 200

    # Адаптер вызван с правильным адресом ответа (chat_id, не user_id).
    assert sent == [("whatsapp", "getvisa", "996700333@c.us", "Это менеджер Медина, помогу вам")]

    conv = asyncio.run(store.get("996700333"))
    assert conv.messages[-1].sender == "manager"
    assert conv.messages[-1].text == "Это менеджер Медина, помогу вам"
    assert conv.messages[-1].status == "sent"               # доставка отслежена
    assert conv.messages[-1].provider_msg_id == "wappi-msg-1"
    assert conv.intercepted is True  # ручная отправка перехватила диалог
    assert conv.assigned_to == "admin"  # диалог закреплён за менеджером


def test_conversation_renders_manager_brief(monkeypatch):
    _clear_memory()
    store = panel_store.get_conversation_store()
    asyncio.run(store.add_message("996700444", "client", "хочу визу в США", channel="whatsapp"))
    asyncio.run(store.update_meta(
        "996700444",
        funnel="visa",
        stage="office",
        qualification={"name": "Алия", "country": "США"},
        ai_summary="Визовый лид. Уже собрано: имя: Алия; страна: США.",
        manager_next_step="Согласовать консультацию в офисе.",
        escalation_reason="Бот ведет клиента к консультации.",
        lead_temperature="warm",
    ))

    client = _auth_client()
    resp = client.get("/admin/conversation/996700444")

    assert resp.status_code == 200
    assert "AI для менеджера" in resp.text
    assert "Согласовать консультацию" in resp.text
    assert "тёплый" in resp.text


def test_manager_brief_marks_hot_payment_signal():
    from app.core.manager_brief import build_manager_brief
    from app.core.state import DialogState

    state = DialogState(
        user_id="hot-1",
        funnel="tours",
        stage="qualification",
        history=[{"role": "user", "content": "можете бронировать, готов оплатить"}],
    )

    brief = build_manager_brief(state)

    assert brief["lead_temperature"] == "hot"
    assert "Горячий клиент" in brief["manager_next_step"]
    assert "готовность" in brief["escalation_reason"]


# ---------------- Wave 1: логин, claim+аудит, исход ----------------
def test_admin_requires_login():
    client = TestClient(main.app, base_url="https://testserver")       # Secure-cookie сессии
    assert client.get("/admin/board/visa").status_code == 401          # без сессии
    bad = client.post("/admin/login", data={"login": "admin", "password": "wrong"})
    assert bad.status_code == 401
    ok = client.post("/admin/login", data={"login": "admin", "password": "frunze"})
    assert ok.status_code == 200
    assert client.get("/admin/board/visa").status_code == 200          # после логина


def test_takeover_assigns_and_audits():
    _clear_memory()
    store = panel_store.get_conversation_store()
    asyncio.run(store.add_message("u-claim-1", "client", "привет", channel="whatsapp", bot_id="getvisa"))
    asyncio.run(store.update_meta("u-claim-1", funnel="visa"))

    client = _auth_client()
    resp = client.post("/admin/conversation/u-claim-1/takeover")
    assert resp.status_code == 200

    conv = asyncio.run(store.get("u-claim-1"))
    assert conv.assigned_to == "admin"      # закреплён за менеджером
    assert conv.intercepted is True
    assert any(a["action"] == "takeover" and a["user_id"] == "u-claim-1"
               for a in panel_store._memory_store._audit)


def test_set_outcome_button():
    _clear_memory()
    store = panel_store.get_conversation_store()
    asyncio.run(store.add_message("u-out-1", "client", "оплатил", channel="whatsapp"))
    asyncio.run(store.update_meta("u-out-1", funnel="tours"))

    client = _auth_client()
    resp = client.post("/admin/conversation/u-out-1/outcome", data={"outcome": "won"})
    assert resp.status_code == 200
    conv = asyncio.run(store.get("u-out-1"))
    assert conv.outcome == "won"


def test_busy_warning_for_other_manager():
    _clear_memory()
    store = panel_store.get_conversation_store()
    asyncio.run(store.add_message("u-busy-1", "client", "вопрос", channel="whatsapp"))
    asyncio.run(store.update_meta("u-busy-1", funnel="visa", assigned_to="medina"))

    client = _auth_client()  # вошли как admin
    resp = client.get("/admin/conversation/u-busy-1")
    assert resp.status_code == 200
    assert "medina" in resp.text and "уже ведёт" in resp.text   # мягкое предупреждение


# ---------------- Wave 3: исход + аналитика ----------------
def test_outcome_manual_sticky_over_auto():
    """Ручной исход (won/lost) не перезатирается авто-исходом из стадии."""
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite://",
                                     connect_args={"check_same_thread": False}, poolclass=StaticPool)
        await init_models(engine)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        store = PostgresConversationStore(sessionmaker=sm)
        await store.add_message("u-w", "client", "оплатил", channel="whatsapp")
        await store.update_meta("u-w", funnel="tours", outcome="won")     # менеджер отметил
        await store.update_meta("u-w", stage="manager", outcome="manager")  # авто из стадии
        conv = await store.get("u-w")
        assert conv.outcome == "won"   # ручной финал устоял
        await engine.dispose()
    asyncio.run(scenario())


def test_compute_analytics_basic():
    from datetime import datetime, timedelta, timezone
    from app.integrations.panel.analytics import compute_analytics
    from app.integrations.panel.store import ConversationView, MessageView

    t0 = datetime(2026, 6, 24, 10, 0, tzinfo=timezone.utc)
    # Диалог 1: только бот (contained), оплатил.
    c1 = ConversationView(user_id="a", funnel="visa", stage="office", outcome="won",
                          messages=[MessageView("client", "привет", t0),
                                    MessageView("bot", "здравствуйте", t0 + timedelta(minutes=1))])
    # Диалог 2: подключался менеджер, ответил через 5 мин.
    c2 = ConversationView(user_id="b", funnel="tours", stage="manager", outcome="manager",
                          intercepted=True, escalation_reason="Готов оплатить",
                          messages=[MessageView("client", "вопрос", t0),
                                    MessageView("manager", "отвечаю", t0 + timedelta(minutes=5))])
    data = compute_analytics([c1, c2])
    assert data["total"] == 2
    assert data["contained"] == 1                 # только c1 без менеджера
    assert data["containment_rate"] == 50
    assert data["outcomes"]["won"] == 1
    assert data["avg_response_min"] == 5.0
    assert data["handoff_reasons"][0] == ("Готов оплатить", 1)


def test_analytics_endpoint_renders():
    _clear_memory()
    store = panel_store.get_conversation_store()
    asyncio.run(store.add_message("u-an-1", "client", "привет", channel="whatsapp"))
    asyncio.run(store.update_meta("u-an-1", funnel="visa", stage="office", outcome="won"))
    client = _auth_client()
    resp = client.get("/admin/analytics")
    assert resp.status_code == 200
    assert "Containment" in resp.text
    assert "Оплатили" in resp.text


def test_analytics_period_and_by_manager():
    """Период принимается, разрез по менеджерам считается по assigned_to."""
    _clear_memory()
    store = panel_store.get_conversation_store()
    asyncio.run(store.add_message("u-an-2", "client", "привет", channel="whatsapp"))
    asyncio.run(store.update_meta("u-an-2", funnel="tours", stage="manager",
                                  outcome="won", assigned_to="sezim"))
    client = _auth_client()
    resp = client.get("/admin/analytics?period=7d")
    assert resp.status_code == 200
    assert "По менеджерам" in resp.text
    assert "sezim" in resp.text


def test_inbox_lists_waiting_across_funnels():
    """Инбокс показывает ждущих ответа клиентов из разных воронок в одном списке."""
    _clear_memory()
    store = panel_store.get_conversation_store()
    asyncio.run(store.add_message("getvisa:996700111", "client", "нужна виза", channel="whatsapp"))
    asyncio.run(store.update_meta("getvisa:996700111", funnel="visa", stage="qualification"))
    asyncio.run(store.add_message("frunze:996700222", "client", "хочу тур", channel="whatsapp"))
    asyncio.run(store.update_meta("frunze:996700222", funnel="tours", stage="qualification"))
    client = _auth_client()
    resp = client.get("/admin/inbox")
    assert resp.status_code == 200
    assert "Ждут ответа" in resp.text
    assert "996700111" in resp.text and "996700222" in resp.text


def test_search_finds_by_phone_and_empty_returns_inbox():
    _clear_memory()
    store = panel_store.get_conversation_store()
    asyncio.run(store.add_message("getvisa:996700333", "client", "вопрос", channel="whatsapp"))
    asyncio.run(store.update_meta("getvisa:996700333", funnel="visa", stage="qualification"))
    client = _auth_client()
    hit = client.get("/admin/search", params={"q": "0333"})
    assert hit.status_code == 200 and "996700333" in hit.text and "Поиск" in hit.text
    miss = client.get("/admin/search", params={"q": "нетакого"})
    assert "Ничего не найдено" in miss.text
    empty = client.get("/admin/search", params={"q": "  "})
    assert "Ждут ответа" in empty.text   # пустой запрос → инбокс


def test_manager_can_move_card_stage():
    """Drag-and-drop: ручной перенос карточки в другую колонку меняет стадию диалога."""
    _clear_memory()
    store = panel_store.get_conversation_store()
    uid = "getvisa:996700555"
    asyncio.run(store.add_message(uid, "client", "привет", channel="whatsapp"))
    asyncio.run(store.update_meta(uid, funnel="visa", stage="qualification"))
    client = _auth_client()

    r = client.post(f"/admin/conversation/{uid}/stage", data={"stage": "office"})
    assert r.status_code == 200 and r.text == "ok"
    assert asyncio.run(store.get(uid)).stage == "office"

    # Доска кладёт карточку в колонку office (стадия-ключ round-trip-ит в свою колонку).
    board = client.get("/admin/board/visa")
    assert board.status_code == 200 and "996700555" in board.text

    # Неизвестная колонка отклоняется.
    bad = client.post(f"/admin/conversation/{uid}/stage", data={"stage": "nope"})
    assert bad.status_code == 400


def test_stats_endpoint_counts_waiting():
    _clear_memory()
    store = panel_store.get_conversation_store()
    asyncio.run(store.add_message("getvisa:996700444", "client", "жду", channel="whatsapp"))
    asyncio.run(store.update_meta("getvisa:996700444", funnel="visa", stage="qualification"))
    client = _auth_client()
    resp = client.get("/admin/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["waiting"] >= 1 and "needs_reply" in data

    # Без сессии — 401 (счётчик не светим публично).
    assert TestClient(main.app).get("/admin/stats").status_code == 401


def test_feature_toggle_buttons():
    """Тумблеры вкл/выкл (автодожим, алерты) меняют рантайм-флаги и переживают рестарт."""
    _clear_memory()
    from app.core import flags
    client = _auth_client()
    # автодожим по умолчанию выключен
    assert "Автодожим" in client.get("/admin/system").text
    r = client.post("/admin/flags/followup_enabled", data={"on": "1"})
    assert r.status_code == 200 and "ВКЛ" in r.text
    assert asyncio.run(flags.get_flag("followup_enabled", False)) is True
    assert "ВКЛ" in client.get("/admin/system").text          # сохранилось
    # выключаем обратно
    assert "ВЫКЛ" in client.post("/admin/flags/followup_enabled", data={"on": "0"}).text
    assert asyncio.run(flags.get_flag("followup_enabled", True)) is False
    # второй тумблер — watchdog-алерты (дефолт вкл)
    assert "Watchdog" in client.get("/admin/system").text
    assert asyncio.run(flags.get_flag("alerts_enabled", True)) is True
    client.post("/admin/flags/alerts_enabled", data={"on": "0"})
    assert asyncio.run(flags.get_flag("alerts_enabled", True)) is False
    # неизвестный флаг → 404
    assert client.post("/admin/flags/nope", data={"on": "1"}).status_code == 404


def test_per_bot_toggle_sets_flag_and_orchestrator_uses_it():
    """Админский тумблер конкретного бота пишет bots_enabled:<id>, оркестратор читает его."""
    _clear_memory()
    from app.core import flags

    asyncio.run(flags.set_flag("bots_enabled", False))
    client = _auth_client()

    resp = client.post("/admin/bots/frunze_tours/toggle", data={"on": "1"})

    assert resp.status_code == 200
    assert "frunze_tours" in resp.text
    assert asyncio.run(flags.get_flag("bots_enabled:frunze_tours", False)) is True
    assert asyncio.run(Orchestrator(
        channel=_FakeChannel(),
        bot=BotConfig(id="frunze_tours", scenario="tours"),
    )._bots_on()) is True
    assert asyncio.run(Orchestrator(
        channel=_FakeChannel(),
        bot=BotConfig(id="getvisa", scenario="visa"),
    )._bots_on()) is False
    assert any(a["action"] == "flag" and "bots_enabled:frunze_tours=on" in a["detail"]
               for a in panel_store._memory_store._audit)

    assert client.post("/admin/bots/nope/toggle", data={"on": "1"}).status_code == 404


def test_system_and_audit_pages():
    _clear_memory()
    store = panel_store.get_conversation_store()
    asyncio.run(store.add_audit("admin", "takeover", "getvisa:1", "перехват"))
    client = _auth_client()
    sysr = client.get("/admin/system")
    assert sysr.status_code == 200 and "Статус системы" in sysr.text
    aud = client.get("/admin/audit")
    assert aud.status_code == 200 and "takeover" in aud.text and "admin" in aud.text
    # без сессии — закрыто
    assert TestClient(main.app).get("/admin/system").status_code == 401
    assert TestClient(main.app).get("/admin/audit").status_code == 401


def test_conversations_separated_by_bot(monkeypatch):
    """Один номер у тур-бота и виза-бота = ДВА отдельных диалога (ключ bot_id:номер)."""
    _clear_memory()
    monkeypatch.setattr("app.agent.llm.settings.openrouter_api_key", "")
    from app.core.state import get_state_store
    store = panel_store.get_conversation_store()
    phone = "996555000111"
    tours = Orchestrator(channel=_FakeChannel(), bot=BotConfig(id="frunze_tours", scenario="tours"))
    visa = Orchestrator(channel=_FakeChannel(), bot=BotConfig(id="getvisa", scenario="visa"))
    asyncio.run(tours.handle(_msg(phone, "хочу тур")))
    asyncio.run(visa.handle(_msg(phone, "хочу визу")))

    t = asyncio.run(store.get(f"frunze_tours:{phone}"))
    v = asyncio.run(store.get(f"getvisa:{phone}"))
    assert t is not None and v is not None
    assert t.funnel == "tours" and v.funnel == "visa"   # воронки не смешались
    assert t.phone == phone and v.phone == phone        # показываем один номер

    # Перехват одного бота НЕ глушит другого (раздельное состояние).
    from app.admin.router import _set_intercept
    asyncio.run(_set_intercept(f"frunze_tours:{phone}", True))
    assert asyncio.run(get_state_store().load(f"getvisa:{phone}")).intercepted is False


# ---------------- быстрый вход для демо ----------------
def test_demo_login_gated_by_setting(monkeypatch):
    # Выключено по умолчанию → эндпоинт недоступен, кнопок нет.
    monkeypatch.setattr("app.config.settings.demo_login", False)
    client = TestClient(main.app, base_url="https://testserver")  # Secure-cookie сессии
    assert "Быстрый вход" not in client.get("/admin/login").text
    assert client.post("/admin/login/demo", data={"login": "admin"}).status_code == 404

    # Включено → кнопки есть и вход без пароля работает.
    monkeypatch.setattr("app.config.settings.demo_login", True)
    page = client.get("/admin/login").text
    assert "Быстрый вход" in page and "Войти как" in page
    r = client.post("/admin/login/demo", data={"login": "admin"})
    assert r.status_code == 200  # редирект на /admin → 200
    assert client.get("/admin/board/visa").status_code == 200  # сессия установлена

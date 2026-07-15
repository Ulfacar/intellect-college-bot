"""Increment 2: канонические Conversation/Lead модели поверх новых таблиц
`pilot_conversations`/`leads` (app/integrations/panel/leadstore.py).

Contract-тесты прогоняются на ДВУХ бэкендах (MemoryLeadStore и PostgresLeadStore на
SQLite-in-memory, по образцу tests/test_crm_postgres.py и tests/test_panel.py — реальный
сетевой Postgres не требуется) и обязаны вести себя идентично.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.integrations.crm.db import Lead as LeadRow
from app.integrations.crm.db import PilotConversation as PilotConversationRow
from app.integrations.crm.db import Conversation as LegacyConversationRow
from app.integrations.crm.db import init_models
from app.integrations.panel.leadstore import (
    MemoryLeadStore,
    PostgresLeadStore,
    dialog_owner_to_intercepted,
    intercepted_to_dialog_owner,
)


async def _pg_store() -> PostgresLeadStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    await init_models(engine)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    return PostgresLeadStore(sessionmaker=sm)


def _run(coro):
    return asyncio.run(coro)


BACKENDS = ["memory", "postgres"]


async def _store(kind: str):
    if kind == "memory":
        return MemoryLeadStore()
    return await _pg_store()


# --------------------------------------------------------------------------------------
# 1. Conversation создаётся отдельно от Lead.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_conversation_created_separately_from_lead(backend):
    async def scenario():
        store = await _store(backend)
        conv = await store.create_conversation(bot_id="college_1", external_user_id="tg-1")
        assert conv.id is not None
        assert conv.lead_id is None  # диалог существует без привязанного лида

    _run(scenario())


# --------------------------------------------------------------------------------------
# 2. Lead существует без Conversation.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_lead_exists_without_conversation(backend):
    async def scenario():
        store = await _store(backend)
        lead = await store.create_lead(name="Без диалога")
        fetched = await store.get_lead(lead.id)
        assert fetched is not None
        assert fetched.name == "Без диалога"
        assert fetched.lead_status == "new"  # дефолт

    _run(scenario())


# --------------------------------------------------------------------------------------
# 3. Conversation связывается с Lead через lead_id.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_conversation_links_to_lead_by_lead_id(backend):
    async def scenario():
        store = await _store(backend)
        lead = await store.create_lead(name="Абитуриент")
        conv = await store.create_conversation(bot_id="college_1", external_user_id="tg-2")
        assert conv.lead_id is None

        await store.link_conversation_to_lead(conv.id, lead.id)

        linked = await store.get_conversation(conv.id)
        assert linked.lead_id == lead.id

    _run(scenario())


# --------------------------------------------------------------------------------------
# 4. get_active_conversation находит активную сессию по bot_id+external_user_id.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_get_active_conversation_finds_session(backend):
    async def scenario():
        store = await _store(backend)
        created = await store.create_conversation(bot_id="college_2", external_user_id="tg-3")

        found = await store.get_active_conversation("college_2", "tg-3")

        assert found is not None
        assert found.id == created.id
        # другой bot_id/юзер не матчится
        assert await store.get_active_conversation("college_3", "tg-3") is None
        assert await store.get_active_conversation("college_2", "tg-nope") is None

    _run(scenario())


# --------------------------------------------------------------------------------------
# 5. Архивная сессия не считается активной.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_archived_session_is_not_active(backend):
    async def scenario():
        store = await _store(backend)
        conv = await store.create_conversation(bot_id="college_1", external_user_id="tg-4")
        assert (await store.get_active_conversation("college_1", "tg-4")) is not None

        await store.archive_conversation(conv.id)

        assert await store.get_active_conversation("college_1", "tg-4") is None
        archived = await store.get_conversation(conv.id)
        assert archived.archived_at is not None
        assert archived.is_active is False

    _run(scenario())


# --------------------------------------------------------------------------------------
# 6. create_new_session сохраняет историю (старый Conversation доступен, но заархивирован).
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_create_new_session_keeps_old_history(backend):
    async def scenario():
        store = await _store(backend)
        old_conv, old_lead = await store.create_new_session(bot_id="college_1", external_user_id="tg-5")

        new_conv, new_lead = await store.create_new_session(bot_id="college_1", external_user_id="tg-5")

        assert new_conv.id != old_conv.id
        assert new_lead.id != old_lead.id
        assert new_conv.archived_at is None
        assert new_conv.lead_id == new_lead.id

        # старая сессия по-прежнему читается (история не удалена), но заархивирована.
        old_again = await store.get_conversation(old_conv.id)
        assert old_again is not None
        assert old_again.archived_at is not None
        assert old_again.lead_id == old_lead.id

        # активная сессия для этого bot_id+user теперь только новая.
        active = await store.get_active_conversation("college_1", "tg-5")
        assert active is not None
        assert active.id == new_conv.id

    _run(scenario())


# --------------------------------------------------------------------------------------
# 7. lead_status хранится в Lead, а не выводится из DialogState.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_lead_status_stored_in_lead_not_dialog_state(backend):
    from app.core.state import DialogState

    async def scenario():
        store = await _store(backend)
        lead = await store.create_lead(lead_status="callback")
        fetched = await store.get_lead(lead.id)
        assert fetched.lead_status == "callback"

        # DialogState не владеет lead_status: поля такого нет в dataclass.
        state = DialogState(user_id="tg-6", stage="qualification")
        assert not hasattr(state, "lead_status")

    _run(scenario())


# --------------------------------------------------------------------------------------
# 8. Legacy stage/intercepted по-прежнему читаемы (DialogState не тронут).
# --------------------------------------------------------------------------------------

def test_legacy_stage_and_intercepted_still_readable():
    from app.core.state import DialogState

    state = DialogState(user_id="tg-7", stage="manager", intercepted=True)

    assert state.stage == "manager"
    assert state.intercepted is True


# --------------------------------------------------------------------------------------
# 9. Mapping helper: intercepted=True <-> dialog_owner=manager (False<->bot).
# --------------------------------------------------------------------------------------

def test_intercepted_dialog_owner_mapping():
    assert intercepted_to_dialog_owner(True) == "manager"
    assert intercepted_to_dialog_owner(False) == "bot"
    assert dialog_owner_to_intercepted("manager") is True
    assert dialog_owner_to_intercepted("bot") is False


# --------------------------------------------------------------------------------------
# 10. paused не ломает legacy-helper (документированный, но детерминированный результат).
# --------------------------------------------------------------------------------------

def test_paused_does_not_break_legacy_mapping_helper():
    result = dialog_owner_to_intercepted("paused")
    assert isinstance(result, bool)  # не бросает, возвращает булево значение


# --------------------------------------------------------------------------------------
# 11. Memory и Postgres реализации дают идентичный контракт (доп. round-trip проверка).
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_and_postgres_share_identical_contract(backend):
    async def scenario():
        store = await _store(backend)
        lead = await store.create_lead(
            name="Контракт", phone="+996700000000", lead_temperature="warm",
        )
        conv = await store.create_conversation(
            bot_id="college_1", external_user_id="tg-8", lead_id=lead.id,
            bot_phase="qualification", dialog_owner="manager",
        )

        fetched_lead = await store.get_lead(lead.id)
        fetched_conv = await store.get_conversation(conv.id)

        assert fetched_lead.name == "Контракт"
        assert fetched_lead.phone == "+996700000000"
        assert fetched_lead.lead_temperature == "warm"
        assert fetched_conv.bot_phase == "qualification"
        assert fetched_conv.dialog_owner == "manager"
        assert fetched_conv.lead_id == lead.id

        updated = await store.update_lead(lead.id, lead_status="in_progress")
        assert updated.lead_status == "in_progress"

    _run(scenario())


# --------------------------------------------------------------------------------------
# 12. Аддитивная схема не удаляет старые таблицы/поля.
# --------------------------------------------------------------------------------------

def test_additive_schema_keeps_legacy_tables_and_columns():
    # Новые таблицы существуют в metadata бок о бок со старыми.
    assert "leads" in LeadRow.metadata.tables
    assert "pilot_conversations" in PilotConversationRow.metadata.tables
    assert "conversations" in LegacyConversationRow.metadata.tables  # legacy НЕ удалена

    legacy_columns = {c.name for c in LegacyConversationRow.__table__.columns}
    # Старые legacy-поля stage/intercepted по-прежнему в таблице conversations.
    assert {"stage", "intercepted", "user_id", "phone"}.issubset(legacy_columns)

    lead_columns = {c.name for c in LeadRow.__table__.columns}
    assert {"lead_status", "lead_source", "lead_temperature"}.issubset(lead_columns)

    conv_columns = {c.name for c in PilotConversationRow.__table__.columns}
    assert {"bot_phase", "dialog_owner", "lead_id", "archived_at"}.issubset(conv_columns)


def test_init_models_creates_new_tables_alongside_legacy():
    """init_models() (create_all, аддитивно) создаёт новые таблицы, не трогая старые."""
    async def scenario():
        engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        await init_models(engine)

        from sqlalchemy import inspect as sa_inspect

        async with engine.begin() as conn:
            names = await conn.run_sync(lambda sync_conn: set(sa_inspect(sync_conn).get_table_names()))

        assert {"conversations", "messages", "deals", "leads", "pilot_conversations"}.issubset(names)
        await engine.dispose()

    _run(scenario())

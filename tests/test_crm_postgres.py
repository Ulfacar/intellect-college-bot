"""Тесты PostgresCrm на in-memory SQLite (без сетевого Postgres).

Проверяем контракт CRMPort: создание сделки, движение по стадии, заметки —
всё с реальной БД (async SQLAlchemy), но в памяти и переживая «сессии».
"""
import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.integrations.crm.db import Deal, init_models
from app.integrations.crm.postgres import PostgresCrm


def run_with_db(scenario):
    """Поднять одноразовую общую in-memory SQLite (StaticPool = одна БД на все
    сессии), создать схему и прогнать `scenario(sm)`."""
    async def _main():
        engine = create_async_engine(
            "sqlite+aiosqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        await init_models(engine)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await scenario(sm)
        finally:
            await engine.dispose()

    asyncio.run(_main())


def test_create_lead_persists_and_returns_id():
    async def scenario(sm):
        crm = PostgresCrm(sessionmaker=sm)
        deal_id = await crm.create_lead({"user_id": "u1"}, "tours", {"destination": "Турция"})
        assert deal_id.isdigit()
        async with sm() as session:
            deal = await session.get(Deal, int(deal_id))
            assert deal.funnel == "tours"
            assert deal.stage == "new"
            assert deal.data["destination"] == "Турция"
            assert deal.user_id == "u1"

    run_with_db(scenario)


def test_update_stage_and_add_note_persist():
    async def scenario(sm):
        crm = PostgresCrm(sessionmaker=sm)
        deal_id = await crm.create_lead({"user_id": "v1"}, "visa", {"country": "Германия"})
        await crm.update_stage(deal_id, "office_consultation")
        await crm.add_note(deal_id, "клиент тёплый")
        await crm.add_note(deal_id, "перезвонить завтра")
        async with sm() as session:
            deal = await session.get(Deal, int(deal_id))
            assert deal.stage == "office_consultation"
            assert deal.notes == ["клиент тёплый", "перезвонить завтра"]

    run_with_db(scenario)


def test_update_unknown_deal_is_safe():
    async def scenario(sm):
        crm = PostgresCrm(sessionmaker=sm)
        # Несуществующий / нечисловой id не должен валить вызов.
        await crm.update_stage("999", "x")
        await crm.update_stage("not-an-int", "x")
        await crm.add_note("999", "ничего")

    run_with_db(scenario)

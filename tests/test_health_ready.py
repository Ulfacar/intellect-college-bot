"""STAGING 1 (owner §3) — readiness: /health/ready и check_db_ready.

check_db_ready проверяется на реальной in-memory SQLite (как tests/test_crm_postgres.py):
готовая схема → True; пустая БД без таблиц → False; сбой соединения → False. Эндпоинт
проверяется через TestClient (без lifespan): memory-бэкенд → 200; postgres + не готова →
503; готова → 200; ответ не раскрывает DATABASE_URL/credentials.
"""
import asyncio

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.main as main
from app.config import settings
from app.integrations.crm.db import check_db_ready, init_models


def _sqlite_sm(with_schema: bool):
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    if with_schema:
        asyncio.run(init_models(engine))
    return async_sessionmaker(engine, expire_on_commit=False), engine


def test_check_db_ready_true_when_schema_present():
    sm, engine = _sqlite_sm(with_schema=True)
    try:
        assert asyncio.run(check_db_ready(sm)) is True
    finally:
        asyncio.run(engine.dispose())


def test_check_db_ready_false_when_tables_missing():
    sm, engine = _sqlite_sm(with_schema=False)   # пустая БД без критичных таблиц
    try:
        assert asyncio.run(check_db_ready(sm)) is False
    finally:
        asyncio.run(engine.dispose())


def test_check_db_ready_false_on_connection_error():
    class _BoomSession:
        async def __aenter__(self):
            raise RuntimeError("db down")
        async def __aexit__(self, *a):
            return False

    def boom_sm():
        return _BoomSession()

    assert asyncio.run(check_db_ready(boom_sm)) is False


def test_health_ready_memory_backend_is_200():
    # дефолтные тестовые настройки: panel/crm = memory → readiness == liveness.
    client = TestClient(main.app, base_url="https://testserver")
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_health_ready_503_when_db_not_ready(monkeypatch):
    monkeypatch.setattr(settings, "panel_backend", "postgres")

    async def not_ready(*a, **k):
        return False
    monkeypatch.setattr("app.integrations.crm.db.check_db_ready", not_ready)

    client = TestClient(main.app, base_url="https://testserver")
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["status"] == "not_ready"


def test_health_ready_200_when_db_ready(monkeypatch):
    monkeypatch.setattr(settings, "panel_backend", "postgres")

    async def ready(*a, **k):
        return True
    monkeypatch.setattr("app.integrations.crm.db.check_db_ready", ready)

    client = TestClient(main.app, base_url="https://testserver")
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_health_ready_does_not_leak_dsn_or_credentials(monkeypatch):
    monkeypatch.setattr(settings, "panel_backend", "postgres")
    monkeypatch.setattr(settings, "database_url",
                        "postgresql+asyncpg://dbuser:SUPERSECRETPW@dbhost:5432/college_staging")

    async def not_ready(*a, **k):
        return False
    monkeypatch.setattr("app.integrations.crm.db.check_db_ready", not_ready)

    client = TestClient(main.app, base_url="https://testserver")
    r = client.get("/health/ready")
    body = r.text
    assert "SUPERSECRETPW" not in body
    assert "postgresql" not in body
    assert "dbuser" not in body
    assert "dbhost" not in body

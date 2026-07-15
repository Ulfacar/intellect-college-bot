"""Outbox store (Increment 3 телеграм-пилота) — additive таблица `outbox`.

Событие ставится ТОЛЬКО при РЕАЛЬНОЙ смене `lead_status` (см.
`app/core/lead_status_service.py::LeadStatusService.set_status`/`apply_invited_handoff`).
НИКОГДА не создаётся для: no-op (target==current), блокировки `manual_status_lock_until`,
смены `dialog_owner`, смены `bot_phase`, зеркалирования legacy-полей, чтения/бэкофилла.

Никакого реального Trello API в Phase 1 (см. docs/phase1-implementation-plan.md §8) —
`process_pending_stub()` ниже НЕ звонит в Trello и НЕ помечает события ошибочными
только из-за отсутствия внешнего consumer'а: события остаются `pending` до тех пор,
пока Phase 2 не подключит реального Trello-воркера отдельной командой. Приложение и
тесты никогда не делают сетевых вызовов через этот модуль.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DuplicateIdempotencyKeyError(Exception):
    """`idempotency_key` уже существует — защита от повторной постановки того же
    события. Memory-бэкенд проверяет это в приложении; Postgres-бэкенд полагается на
    `UNIQUE`-индекс `outbox.idempotency_key` (см. `migrations/0002_...sql`) и
    переупаковывает `IntegrityError` в это же исключение для единообразного контракта.
    """


@dataclass
class OutboxRecord:
    id: int
    aggregate_type: str = "lead"
    aggregate_id: int = 0
    event_type: str = "lead_status_changed"
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    status: str = "pending"          # pending|processed|error
    attempts: int = 0
    created_at: datetime | None = None
    processed_at: datetime | None = None
    last_error: str | None = None


class MemoryOutboxStore:
    """Outbox в памяти процесса (дефолт: тесты, офлайн)."""

    def __init__(self) -> None:
        self._rows: dict[int, OutboxRecord] = {}
        self._seq = 0
        self._keys: set[str] = set()

    def exists(self, idempotency_key: str) -> bool:
        """Не-async проверка занятости ключа — используется `LeadStatusService`
        как pre-flight проверка ДО мутации в атомарном memory-пути (см.
        `lead_status_service.py::_apply_atomic_memory`), чтобы outbox-запись была
        ПЕРВОЙ мутацией: при дубликате ключа ничего другого ещё не тронуто."""
        return idempotency_key in self._keys

    async def create(
        self, *, aggregate_id: int, idempotency_key: str, aggregate_type: str = "lead",
        event_type: str = "lead_status_changed", payload: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> OutboxRecord:
        if idempotency_key in self._keys:
            raise DuplicateIdempotencyKeyError(idempotency_key)
        self._seq += 1
        row = OutboxRecord(
            id=self._seq, aggregate_type=aggregate_type, aggregate_id=aggregate_id, event_type=event_type,
            payload=dict(payload or {}), idempotency_key=idempotency_key, status="pending", attempts=0,
            created_at=created_at or _now(),
        )
        self._rows[row.id] = row
        self._keys.add(idempotency_key)
        return row

    async def get(self, record_id: int) -> OutboxRecord | None:
        return self._rows.get(record_id)

    async def list_pending(self) -> list[OutboxRecord]:
        return [r for r in self._rows.values() if r.status == "pending"]

    async def list_for_aggregate(self, aggregate_id: int) -> list[OutboxRecord]:
        return [r for r in self._rows.values() if r.aggregate_id == aggregate_id]

    async def mark_processed(self, record_id: int, *, now: datetime | None = None) -> None:
        row = self._rows.get(record_id)
        if row is not None:
            row.status = "processed"
            row.processed_at = now or _now()

    async def mark_error(self, record_id: int, error: str) -> None:
        row = self._rows.get(record_id)
        if row is not None:
            row.attempts += 1
            row.last_error = error


class PostgresOutboxStore:
    """Outbox в Postgres (прод). sessionmaker инъектируется в тестах."""

    def __init__(self, sessionmaker: async_sessionmaker | None = None) -> None:
        self._sessionmaker = sessionmaker

    def _sm(self) -> async_sessionmaker:
        if self._sessionmaker is None:
            from app.integrations.crm.db import get_sessionmaker
            self._sessionmaker = get_sessionmaker()
        return self._sessionmaker

    def sessionmaker(self) -> async_sessionmaker:
        return self._sm()

    async def create(
        self, *, aggregate_id: int, idempotency_key: str, aggregate_type: str = "lead",
        event_type: str = "lead_status_changed", payload: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> OutboxRecord:
        from sqlalchemy.exc import IntegrityError

        from app.integrations.crm.db import Outbox
        async with self._sm()() as session:
            row = Outbox(
                aggregate_type=aggregate_type, aggregate_id=aggregate_id, event_type=event_type,
                payload=dict(payload or {}), idempotency_key=idempotency_key, status="pending", attempts=0,
                created_at=created_at or _now(),
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise DuplicateIdempotencyKeyError(idempotency_key) from exc
            await session.refresh(row)
            return _outbox_view(row)

    async def get(self, record_id: int) -> OutboxRecord | None:
        from app.integrations.crm.db import Outbox
        async with self._sm()() as session:
            row = await session.get(Outbox, record_id)
            return _outbox_view(row) if row is not None else None

    async def list_pending(self) -> list[OutboxRecord]:
        from sqlalchemy import select

        from app.integrations.crm.db import Outbox
        async with self._sm()() as session:
            rows = (
                await session.execute(select(Outbox).where(Outbox.status == "pending"))
            ).scalars().all()
            return [_outbox_view(r) for r in rows]

    async def list_for_aggregate(self, aggregate_id: int) -> list[OutboxRecord]:
        from sqlalchemy import select

        from app.integrations.crm.db import Outbox
        async with self._sm()() as session:
            rows = (
                await session.execute(select(Outbox).where(Outbox.aggregate_id == aggregate_id))
            ).scalars().all()
            return [_outbox_view(r) for r in rows]

    async def mark_processed(self, record_id: int, *, now: datetime | None = None) -> None:
        from app.integrations.crm.db import Outbox
        async with self._sm()() as session:
            row = await session.get(Outbox, record_id)
            if row is not None:
                row.status = "processed"
                row.processed_at = now or _now()
                await session.commit()

    async def mark_error(self, record_id: int, error: str) -> None:
        from app.integrations.crm.db import Outbox
        async with self._sm()() as session:
            row = await session.get(Outbox, record_id)
            if row is not None:
                row.attempts += 1
                row.last_error = error
                await session.commit()


def _outbox_view(row) -> OutboxRecord:
    return OutboxRecord(
        id=row.id, aggregate_type=row.aggregate_type, aggregate_id=row.aggregate_id, event_type=row.event_type,
        payload=dict(row.payload or {}), idempotency_key=row.idempotency_key, status=row.status,
        attempts=row.attempts, created_at=row.created_at, processed_at=row.processed_at,
        last_error=row.last_error,
    )


_memory_outbox_store = MemoryOutboxStore()
_pg_outbox_store: PostgresOutboxStore | None = None


def get_outbox_store():
    """Сконфигурированный бэкенд (singleton), тот же переключатель, что у
    `get_lead_store()`/`get_audit_store()` (`settings.panel_backend`)."""
    global _pg_outbox_store
    if settings.panel_backend == "postgres":
        if _pg_outbox_store is None:
            _pg_outbox_store = PostgresOutboxStore()
        return _pg_outbox_store
    return _memory_outbox_store


async def process_pending_stub(outbox_store=None, *, limit: int = 50) -> int:
    """No-op stub worker (Phase 1 / Increment 3). НЕ звонит в Trello, НЕ помечает
    события ошибочными только из-за отсутствия внешнего consumer'а — события остаются
    `pending`. Возвращает количество увиденных pending-событий (только для
    наблюдаемости/логов). Реальный Trello-worker — Phase 2, отдельной командой (см.
    docs/phase1-implementation-plan.md §8).
    """
    store = outbox_store or get_outbox_store()
    pending = await store.list_pending()
    return len(pending[:limit])

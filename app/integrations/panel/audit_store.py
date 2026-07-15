"""Audit trail store (Increment 3 телеграм-пилота) — additive таблица `lead_audit`.

Пишет `LeadStatusService` (`lead_status_changed`/`status_change_blocked`) и
`ConversationService` (`dialog_owner_changed`); `bot_phase_changed` зарезервирован на
будущее (не используется в Increment 3, т.к. `bot_phase` меняет только
`ConversationService`/`update_conversation`, отдельного метода под него в этом
инкременте нет). Два бэкенда за одним контрактом (по образцу `leadstore.py`):
`MemoryAuditStore` (дефолт) / `PostgresAuditStore` (прод). Никаких секретов, полных
промптов или лишней PII — только коды статусов/owner и короткие reason/actor строки.

ВАЖНО про транзакционность: `record()` (и `insert()`) каждый раз сами открывают и
коммитят свою сессию — это ок для САМОСТОЯТЕЛЬНЫХ записей аудита (например,
`dialog_owner_changed` из `conversation_service.py`, где нет требования к общей
транзакции с другой таблицей). Для АТОМАРНОЙ смены `lead_status` (lead + audit + outbox
в ОДНОЙ транзакции) `LeadStatusService` для Postgres-бэкенда НЕ вызывает публичные
методы этого модуля — пишет `LeadAudit`/`Outbox` напрямую в своей собственной
сессии/транзакции (см. `app/core/lead_status_service.py::_apply_atomic_postgres`).
Для memory-бэкенда `LeadStatusService` использует низкоуровневую пару
`reserve_id()`/`insert()` ниже, чтобы посчитать `idempotency_key` outbox-события ДО
любой мутации (см. `_apply_atomic_memory`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings

EVENT_TYPES = {
    "lead_status_changed", "dialog_owner_changed", "bot_phase_changed", "status_change_blocked",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AuditRecord:
    id: int
    lead_id: int | None = None
    conversation_id: int | None = None
    event_type: str = ""
    previous_status: str | None = None
    new_status: str | None = None
    previous_owner: str | None = None
    new_owner: str | None = None
    source: str = ""
    actor: str | None = None
    reason: str | None = None
    confidence: float | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime | None = None


class MemoryAuditStore:
    """Аудит в памяти процесса (дефолт: тесты, офлайн)."""

    def __init__(self) -> None:
        self._rows: dict[int, AuditRecord] = {}
        self._seq = 0

    def reserve_id(self) -> int:
        """Зарезервировать следующий id БЕЗ вставки строки (нужен `LeadStatusService`
        заранее, чтобы построить `idempotency_key` outbox-события до мутации таблиц —
        см. модульный докстринг). Гэпы в id при неиспользованной резервации — ожидаемы
        (как auto-increment после rollback в настоящей БД)."""
        self._seq += 1
        return self._seq

    async def insert(self, record_id: int, **fields: Any) -> AuditRecord:
        """Вставить строку с уже зарезервированным id (внутренний метод для
        атомарного пути `LeadStatusService`, не для произвольного вызова)."""
        created_at = fields.pop("created_at", None) or _now()
        row = AuditRecord(id=record_id, created_at=created_at, **fields)
        self._rows[record_id] = row
        return row

    async def record(self, **fields: Any) -> AuditRecord:
        """Самостоятельная (не транзакционная) запись аудита — например,
        `dialog_owner_changed` из `conversation_service.py`."""
        record_id = self.reserve_id()
        return await self.insert(record_id, **fields)

    async def get(self, record_id: int) -> AuditRecord | None:
        return self._rows.get(record_id)

    async def list_for_lead(self, lead_id: int) -> list[AuditRecord]:
        return [r for r in self._rows.values() if r.lead_id == lead_id]

    async def list_by_event_type(self, event_type: str) -> list[AuditRecord]:
        return [r for r in self._rows.values() if r.event_type == event_type]


class PostgresAuditStore:
    """Аудит в Postgres (прод). sessionmaker инъектируется в тестах."""

    def __init__(self, sessionmaker: async_sessionmaker | None = None) -> None:
        self._sessionmaker = sessionmaker

    def _sm(self) -> async_sessionmaker:
        if self._sessionmaker is None:
            from app.integrations.crm.db import get_sessionmaker
            self._sessionmaker = get_sessionmaker()
        return self._sessionmaker

    def sessionmaker(self) -> async_sessionmaker:
        return self._sm()

    async def record(self, **fields: Any) -> AuditRecord:
        from app.integrations.crm.db import LeadAudit
        created_at = fields.pop("created_at", None) or _now()
        async with self._sm()() as session:
            row = LeadAudit(created_at=created_at, **_map_metadata(fields))
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _audit_view(row)

    async def get(self, record_id: int) -> AuditRecord | None:
        from app.integrations.crm.db import LeadAudit
        async with self._sm()() as session:
            row = await session.get(LeadAudit, record_id)
            return _audit_view(row) if row is not None else None

    async def list_for_lead(self, lead_id: int) -> list[AuditRecord]:
        from sqlalchemy import select

        from app.integrations.crm.db import LeadAudit
        async with self._sm()() as session:
            rows = (
                await session.execute(select(LeadAudit).where(LeadAudit.lead_id == lead_id))
            ).scalars().all()
            return [_audit_view(r) for r in rows]

    async def list_by_event_type(self, event_type: str) -> list[AuditRecord]:
        from sqlalchemy import select

        from app.integrations.crm.db import LeadAudit
        async with self._sm()() as session:
            rows = (
                await session.execute(select(LeadAudit).where(LeadAudit.event_type == event_type))
            ).scalars().all()
            return [_audit_view(r) for r in rows]


def _map_metadata(fields: dict[str, Any]) -> dict[str, Any]:
    """`metadata` (публичное имя поля контракта) -> `metadata_` (имя ORM-атрибута;
    `metadata` зарезервировано `DeclarativeBase`, см. `crm/db.py::LeadAudit`)."""
    fields = dict(fields)
    if "metadata" in fields:
        fields["metadata_"] = fields.pop("metadata")
    return fields


def _audit_view(row) -> AuditRecord:
    return AuditRecord(
        id=row.id, lead_id=row.lead_id, conversation_id=row.conversation_id, event_type=row.event_type,
        previous_status=row.previous_status, new_status=row.new_status, previous_owner=row.previous_owner,
        new_owner=row.new_owner, source=row.source, actor=row.actor, reason=row.reason,
        confidence=row.confidence, metadata=row.metadata_, created_at=row.created_at,
    )


_memory_audit_store = MemoryAuditStore()
_pg_audit_store: PostgresAuditStore | None = None


def get_audit_store():
    """Сконфигурированный бэкенд (singleton), тот же переключатель, что у
    `get_lead_store()`/`get_conversation_store()` (`settings.panel_backend`)."""
    global _pg_audit_store
    if settings.panel_backend == "postgres":
        if _pg_audit_store is None:
            _pg_audit_store = PostgresAuditStore()
        return _pg_audit_store
    return _memory_audit_store

"""LeadStatusService (Increment 3 телеграм-пилота): единственная точка смены
`lead_status`.

Единственный вход для управляемой (bot/admin/system/trello) смены канонического
`lead_status` (см. `docs/phase1-implementation-plan.md` §3,
`docs/admin-bot-control-and-ai-classification-spec.md` §7-9/§15). Правила переходов —
чистые данные без побочных эффектов (`SOURCE_RIGHTS`, `BOT_TRANSITIONS`,
`MANAGER_ONLY`, `is_transition_allowed`), переиспользуемые также `apply_invited_handoff`
(ниже) для детерминированной проверки допустимости авто-перехода бота в `invited`.

Транзакционность (Postgres): ОДНА сессия/транзакция на `set_status`/
`apply_invited_handoff` — обновление `lead_status` (+`pilot_conversations` для
handoff) + вставка audit-строки + вставка outbox-строки -> один commit; при любой
ошибке -> rollback, БЕЗ частичного изменения. Внутри транзакции НЕТ сетевых вызовов
(Trello и т.п. не зовём — см. `outbox_store.py`).

Memory-бэкенд эмулирует all-or-nothing: всё, что нужно для записи (id аудита,
idempotency_key), считается ЗАРАНЕЕ, точка возможного сбоя (`_raise_before_commit`,
тестовый параметр) проверяется ДО первой мутации, а сама мутация трёх сторов идёт
одним синхронным блоком без промежуточных await к другим сторонам — так что при сбое
НИЧЕГО не применяется.

`apply_invited_handoff` НЕ подключён к LLM/runner/orchestrator в этом инкременте —
только сервис-слой и тесты. Он реализован здесь (а не в `conversation_service.py`),
т.к. переиспользует ту же атомарную multi-table-запись, что и `set_status`, а
`self._lead_store` (см. `leadstore.py`) уже умеет работать и с `Lead`, и с
`PilotConversation` одним и тем же объектом-стором.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.integrations.panel.audit_store import get_audit_store
from app.integrations.panel.leadstore import (
    LEAD_STATUSES,
    UNSET,
    PostgresLeadStore,
    get_lead_store,
)
from app.integrations.panel.outbox_store import get_outbox_store

# --------------------------------------------------------------------------------------
# Чистые переиспользуемые правила переходов (без сайд-эффектов, без I/O).
# --------------------------------------------------------------------------------------

#: Статусы, которые бот НИКОГДА не ставит автоматически ни из какого prev-статуса —
#: только менеджер/admin (trello тоже "manual", см. SOURCE_RIGHTS).
MANAGER_ONLY: set[str] = {"tested_thinking", "pre_contract", "contract", "rejected", "invalid_number"}

#: Статусы, которые бот может пытаться ставить (не все переходы разрешены — см. BOT_TRANSITIONS).
BOT_AUTO_STATUSES: set[str] = {"new", "in_progress", "info_sent", "callback", "thinking", "invited"}

assert not (MANAGER_ONLY & BOT_AUTO_STATUSES), "MANAGER_ONLY и BOT_AUTO_STATUSES не пересекаются"
assert (MANAGER_ONLY | BOT_AUTO_STATUSES) == LEAD_STATUSES, "вместе покрывают все 11 статусов"

#: Разрешённые переходы бота (prev, target) — ровно по ТЗ Increment 3, ничего сверх.
BOT_TRANSITIONS: set[tuple[str, str]] = {
    ("new", "in_progress"),
    ("new", "info_sent"), ("in_progress", "info_sent"),
    ("new", "callback"), ("in_progress", "callback"), ("info_sent", "callback"),
    ("thinking", "callback"), ("callback", "callback"),
    ("new", "thinking"), ("in_progress", "thinking"), ("info_sent", "thinking"), ("callback", "thinking"),
    ("new", "invited"), ("in_progress", "invited"), ("info_sent", "invited"),
    ("callback", "invited"), ("thinking", "invited"),
    ("callback", "in_progress"), ("thinking", "in_progress"),
}

#: system — минимальный, ЯВНО документированный набор технических переходов, а не
#: безлимитный bypass. В Increment 3 нет ни одного конкретного авто-технического
#: сценария смены `lead_status`, поэтому system переиспользует безопасный набор бота
#: (НЕ безлимитные admin/trello права) — расширять точечно, когда появится реальный
#: технический флоу (например, нормализация после бэкофилла).
SYSTEM_TRANSITIONS: set[tuple[str, str]] = set(BOT_TRANSITIONS)

#: Права источников: "auto" — только по таблице переходов + запрет MANAGER_ONLY целей;
#: "manual" — любой из 11 статусов (admin и trello — ТЗ: "trello rights = admin/manual",
#: trello зарезервирован, реально ничего не вызывает source=trello в Increment 3);
#: "system" — только SYSTEM_TRANSITIONS (тоже без MANAGER_ONLY).
SOURCE_RIGHTS: dict[str, str] = {
    "bot": "auto",
    "admin": "manual",
    "trello": "manual",
    "system": "system",
}

MANUAL_LOCK_MINUTES = 30


def is_transition_allowed(source: str, prev_status: str, target_status: str) -> bool:
    """Чистая проверка допустимости `prev_status -> target_status` для `source`.

    НЕ проверяет `manual_status_lock_until` (зависит от времени/состояния лида, а не
    только от source/prev/target — отдельная проверка в `set_status`/
    `apply_invited_handoff`) и НЕ проверяет валидность самих значений статуса/источника
    (это делает вызывающий код до обращения сюда — `invalid_status`/`invalid_source`).
    """
    rights = SOURCE_RIGHTS.get(source)
    if rights == "manual":
        return True
    if rights == "auto":
        if target_status in MANAGER_ONLY:
            return False
        return (prev_status, target_status) in BOT_TRANSITIONS
    if rights == "system":
        if target_status in MANAGER_ONLY:
            return False
        return (prev_status, target_status) in SYSTEM_TRANSITIONS
    return False


@dataclass
class StatusChangeResult:
    changed: bool
    previous_status: str | None
    current_status: str | None
    rejected_reason: str | None = None
    outbox_event_id: str | int | None = None
    manual_lock_until: datetime | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware_utc(value: datetime | None) -> datetime | None:
    """Нормализует datetime к tz-aware UTC.

    Некоторые бэкенды (в частности SQLite/aiosqlite, используемый как "Postgres
    contract" в тестах — см. `tests/test_lead_status_service.py`) не сохраняют tzinfo
    для `DateTime(timezone=True)`-колонок при round-trip и возвращают naive datetime.
    Реальный Postgres (TIMESTAMPTZ) так не делает, но сравнение naive/aware datetime
    бросает `TypeError`, поэтому нормализуем защитно на границе сервиса (весь проект
    трактует datetime как UTC — см. `_now()` во всех модулях)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class LeadStatusService:
    """Единая точка смены `lead_status` (+ атомарный `apply_invited_handoff`)."""

    def __init__(self, lead_store=None, audit_store=None, outbox_store=None) -> None:
        self._lead_store = lead_store if lead_store is not None else get_lead_store()
        self._audit_store = audit_store if audit_store is not None else get_audit_store()
        self._outbox_store = outbox_store if outbox_store is not None else get_outbox_store()

    def _is_postgres(self) -> bool:
        return isinstance(self._lead_store, PostgresLeadStore)

    # ------------------------------------------------------------------------------
    # set_status
    # ------------------------------------------------------------------------------

    async def set_status(
        self, lead_id: int, target_status: str, source: str, *,
        actor: str | None = None, reason: str | None = None, confidence: float | None = None,
        now: datetime | None = None, conversation_id: int | None = None,
        suggested_status: str | None = None, force: bool = False,
        _raise_before_commit: bool = False,
    ) -> StatusChangeResult:
        now = now or _now()

        if source not in SOURCE_RIGHTS:
            return StatusChangeResult(False, None, None, rejected_reason="invalid_source")
        if target_status not in LEAD_STATUSES:
            return StatusChangeResult(False, None, None, rejected_reason="invalid_status")

        lead = await self._lead_store.get_lead(lead_id)
        if lead is None:
            return StatusChangeResult(False, None, None, rejected_reason="lead_not_found")

        previous_status = lead.lead_status
        lock_until = _as_aware_utc(lead.manual_status_lock_until)

        # No-op: target == current. НЕ создаёт outbox / lead_status_changed-аудит.
        # suggested_status при этом МОЖЕТ сохраняться отдельно (без смены lead_status).
        if target_status == previous_status:
            if suggested_status is not None:
                await self._lead_store.update_lead(lead_id, suggested_status=suggested_status)
            return StatusChangeResult(
                False, previous_status, previous_status, rejected_reason=None, manual_lock_until=lock_until,
            )

        is_manual = source in ("admin", "trello")
        lock_active = lock_until is not None and lock_until > now

        # Manual lock блокирует НЕ-ручные источники, если не передан force=True.
        if lock_active and not is_manual and not force:
            if suggested_status is not None:
                await self._lead_store.update_lead(lead_id, suggested_status=suggested_status)
            await self._write_blocked_audit(
                lead_id=lead_id, conversation_id=conversation_id, previous_status=previous_status,
                target_status=target_status, source=source, actor=actor, reason="manual_lock",
                confidence=confidence, now=now,
            )
            return StatusChangeResult(
                False, previous_status, previous_status, rejected_reason="manual_lock", manual_lock_until=lock_until,
            )

        # Права/таблица переходов. force обходит ТОЛЬКО lock (выше), НЕ таблицу переходов.
        if not is_transition_allowed(source, previous_status, target_status):
            reason_code = "manager_only" if target_status in MANAGER_ONLY else "transition_not_allowed"
            await self._write_blocked_audit(
                lead_id=lead_id, conversation_id=conversation_id, previous_status=previous_status,
                target_status=target_status, source=source, actor=actor, reason=reason_code,
                confidence=confidence, now=now,
            )
            return StatusChangeResult(
                False, previous_status, previous_status, rejected_reason=reason_code, manual_lock_until=lock_until,
            )

        new_lock_until = now + timedelta(minutes=MANUAL_LOCK_MINUTES) if is_manual else UNSET

        audit_id, outbox_id, _key = await self._apply_atomic(
            lead_id=lead_id, conversation_id=conversation_id, previous_status=previous_status,
            target_status=target_status, source=source, actor=actor, reason=reason, confidence=confidence,
            now=now, lock_update=new_lock_until, conversation_updates=None,
            _raise_before_commit=_raise_before_commit,
        )

        return StatusChangeResult(
            True, previous_status, target_status, rejected_reason=None, outbox_event_id=outbox_id,
            manual_lock_until=(new_lock_until if is_manual else lock_until),
        )

    async def _write_blocked_audit(
        self, *, lead_id: int, conversation_id: int | None, previous_status: str, target_status: str,
        source: str, actor: str | None, reason: str, confidence: float | None, now: datetime,
    ) -> None:
        await self._audit_store.record(
            lead_id=lead_id, conversation_id=conversation_id, event_type="status_change_blocked",
            previous_status=previous_status, new_status=target_status, source=source, actor=actor,
            reason=reason, confidence=confidence, metadata=None, created_at=now,
        )

    # ------------------------------------------------------------------------------
    # apply_invited_handoff
    # ------------------------------------------------------------------------------

    async def apply_invited_handoff(
        self, lead_id: int, conversation_id: int, *, actor: str | None = None,
        reason: str | None = None, confidence: float | None = None, now: datetime | None = None,
        manager: str | None = None, _raise_before_commit: bool = False,
    ) -> StatusChangeResult:
        """Атомарно (одна транзакция): `lead.lead_status=invited`,
        `conversation.bot_phase=handoff`, `conversation.dialog_owner=manager`. Всё или
        ничего. Ровно ОДНО outbox-событие (для смены `lead_status`). Переиспользует
        `is_transition_allowed` (bot -> invited разрешён из new/in_progress/info_sent/
        callback/thinking). НЕ подключено к LLM/runner — вызывается явно
        application-слоем, когда появится реальный intent `wants_to_come` (будущий
        классификатор, не в этом инкременте)."""
        now = now or _now()
        source = "bot"
        target_status = "invited"

        lead = await self._lead_store.get_lead(lead_id)
        if lead is None:
            return StatusChangeResult(False, None, None, rejected_reason="lead_not_found")
        conv = await self._lead_store.get_conversation(conversation_id)
        if conv is None:
            return StatusChangeResult(
                False, lead.lead_status, lead.lead_status, rejected_reason="conversation_not_found",
            )

        previous_status = lead.lead_status
        lock_until = _as_aware_utc(lead.manual_status_lock_until)

        if previous_status == target_status:
            # Уже invited — идемпотентный no-op (без outbox/аудита), как и set_status.
            return StatusChangeResult(
                False, previous_status, previous_status, rejected_reason=None, manual_lock_until=lock_until,
            )

        lock_active = lock_until is not None and lock_until > now
        if lock_active:
            await self._write_blocked_audit(
                lead_id=lead_id, conversation_id=conversation_id, previous_status=previous_status,
                target_status=target_status, source=source, actor=actor, reason="manual_lock",
                confidence=confidence, now=now,
            )
            return StatusChangeResult(
                False, previous_status, previous_status, rejected_reason="manual_lock", manual_lock_until=lock_until,
            )

        if not is_transition_allowed(source, previous_status, target_status):
            reason_code = "manager_only" if target_status in MANAGER_ONLY else "transition_not_allowed"
            await self._write_blocked_audit(
                lead_id=lead_id, conversation_id=conversation_id, previous_status=previous_status,
                target_status=target_status, source=source, actor=actor, reason=reason_code,
                confidence=confidence, now=now,
            )
            return StatusChangeResult(
                False, previous_status, previous_status, rejected_reason=reason_code, manual_lock_until=lock_until,
            )

        conversation_updates: dict[str, Any] = {"bot_phase": "handoff", "dialog_owner": "manager"}
        if manager:
            conversation_updates["assigned_to"] = manager

        audit_id, outbox_id, _key = await self._apply_atomic(
            lead_id=lead_id, conversation_id=conversation_id, previous_status=previous_status,
            target_status=target_status, source=source, actor=actor, reason=reason, confidence=confidence,
            now=now, lock_update=UNSET, conversation_updates=conversation_updates,
            _raise_before_commit=_raise_before_commit,
        )

        return StatusChangeResult(
            True, previous_status, target_status, rejected_reason=None, outbox_event_id=outbox_id,
            manual_lock_until=lock_until,
        )

    # ------------------------------------------------------------------------------
    # Атомарная запись: lead(+conversation) + audit + outbox, одна транзакция/эмуляция.
    # ------------------------------------------------------------------------------

    async def _apply_atomic(
        self, *, lead_id: int, conversation_id: int | None, previous_status: str, target_status: str,
        source: str, actor: str | None, reason: str | None, confidence: float | None, now: datetime,
        lock_update: Any, conversation_updates: dict[str, Any] | None, _raise_before_commit: bool,
    ) -> tuple[int, int, str]:
        payload = {
            "lead_id": lead_id, "previous_status": previous_status, "target_status": target_status,
            "source": source, "actor": actor, "reason": reason,
        }
        kwargs: dict[str, Any] = dict(
            lead_id=lead_id, conversation_id=conversation_id, previous_status=previous_status,
            target_status=target_status, source=source, actor=actor, reason=reason, confidence=confidence,
            now=now, lock_update=lock_update, conversation_updates=conversation_updates, payload=payload,
            _raise_before_commit=_raise_before_commit,
        )
        if self._is_postgres():
            return await self._apply_atomic_postgres(**kwargs)
        return await self._apply_atomic_memory(**kwargs)

    async def _apply_atomic_memory(
        self, *, lead_id, conversation_id, previous_status, target_status, source, actor, reason,
        confidence, now, lock_update, conversation_updates, payload, _raise_before_commit,
    ) -> tuple[int, int, str]:
        # 1. Заранее считаем всё, что нужно для outbox-ключа (audit_id), но НЕ мутируем.
        audit_id = self._audit_store.reserve_id()
        idempotency_key = f"lead:{lead_id}:status:{target_status}:change:{audit_id}"

        if self._outbox_store.exists(idempotency_key):
            # Практически недостижимо (audit_id уникален на процесс), но защищает
            # инвариант "outbox — первая мутация, при сбое ничего не применяется".
            raise ValueError(f"outbox idempotency_key уже существует: {idempotency_key}")

        if _raise_before_commit:
            raise RuntimeError("injected failure (test-only, _raise_before_commit)")

        # 2. Мутация "в конце": outbox первым (см. exists()-проверку выше), затем
        # lead/conversation, затем аудит — без await к чужим сторонам между шагами.
        outbox_row = await self._outbox_store.create(
            aggregate_type="lead", aggregate_id=lead_id, event_type="lead_status_changed",
            payload=payload, idempotency_key=idempotency_key, created_at=now,
        )
        await self._lead_store.update_lead(
            lead_id, lead_status=target_status, status_change_source=source,
            status_change_by=actor, status_change_reason=reason, manual_status_lock_until=lock_update,
        )
        if conversation_id is not None and conversation_updates:
            await self._lead_store.update_conversation(conversation_id, **conversation_updates)
        await self._audit_store.insert(
            audit_id, lead_id=lead_id, conversation_id=conversation_id, event_type="lead_status_changed",
            previous_status=previous_status, new_status=target_status, source=source, actor=actor,
            reason=reason, confidence=confidence, metadata=None, created_at=now,
        )
        return audit_id, outbox_row.id, idempotency_key

    async def _apply_atomic_postgres(
        self, *, lead_id, conversation_id, previous_status, target_status, source, actor, reason,
        confidence, now, lock_update, conversation_updates, payload, _raise_before_commit,
    ) -> tuple[int, int, str]:
        from app.integrations.crm.db import Lead as LeadRow
        from app.integrations.crm.db import LeadAudit as LeadAuditRow
        from app.integrations.crm.db import Outbox as OutboxRow
        from app.integrations.crm.db import PilotConversation as PilotConversationRow

        sm = self._lead_store.sessionmaker()
        async with sm() as session:
            async with session.begin():
                lead_row = await session.get(LeadRow, lead_id)
                if lead_row is None:
                    raise LookupError(f"lead {lead_id} исчез внутри транзакции")
                lead_row.lead_status = target_status
                lead_row.status_change_source = source
                lead_row.status_change_by = actor
                lead_row.status_change_reason = reason
                if lock_update is not UNSET:
                    lead_row.manual_status_lock_until = lock_update

                if conversation_id is not None and conversation_updates:
                    conv_row = await session.get(PilotConversationRow, conversation_id)
                    if conv_row is not None:
                        for key, value in conversation_updates.items():
                            setattr(conv_row, key, value)

                audit_row = LeadAuditRow(
                    lead_id=lead_id, conversation_id=conversation_id, event_type="lead_status_changed",
                    previous_status=previous_status, new_status=target_status, source=source, actor=actor,
                    reason=reason, confidence=confidence, metadata_=None, created_at=now,
                )
                session.add(audit_row)
                await session.flush()  # присваивает audit_row.id

                idempotency_key = f"lead:{lead_id}:status:{target_status}:change:{audit_row.id}"
                outbox_row = OutboxRow(
                    aggregate_type="lead", aggregate_id=lead_id, event_type="lead_status_changed",
                    payload=payload, idempotency_key=idempotency_key, status="pending", attempts=0,
                    created_at=now,
                )
                session.add(outbox_row)
                await session.flush()

                if _raise_before_commit:
                    raise RuntimeError("injected failure (test-only, _raise_before_commit)")

            # `async with session.begin()` закоммитил транзакцию на выходе без ошибки.
            return audit_row.id, outbox_row.id, idempotency_key

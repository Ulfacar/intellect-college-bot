"""Increment 3: LeadStatusService, ConversationService, audit/outbox сторы.

Contract-тесты прогоняются на ДВУХ бэкендах (Memory и Postgres на SQLite-in-memory, по
образцу tests/test_leadstore.py — реальный сетевой Postgres не требуется) там, где это
осмысленно (основной атомарный путь set_status/apply_invited_handoff); чисто сервисные
сценарии (ConversationService, чистые правила переходов) — на memory (сама логика
backend-агностична, дублировать на PG не даёт нового сигнала).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.conversation_service import ConversationService
from app.core.lead_status_service import (
    BOT_TRANSITIONS,
    MANAGER_ONLY,
    SOURCE_RIGHTS,
    SYSTEM_TRANSITIONS,
    LeadStatusService,
    is_transition_allowed,
)
from app.integrations.crm.db import init_models
from app.integrations.panel.audit_store import MemoryAuditStore, PostgresAuditStore
from app.integrations.panel.leadstore import (
    LEAD_STATUSES,
    ConflictError,
    MemoryLeadStore,
    PostgresLeadStore,
    dialog_owner_to_intercepted,
    intercepted_to_dialog_owner,
)
from app.integrations.panel.outbox_store import MemoryOutboxStore, PostgresOutboxStore


def _run(coro):
    return asyncio.run(coro)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _naive(value: datetime) -> datetime:
    """SQLite (используемая как "Postgres contract" бэкенд в этих тестах) не
    сохраняет tzinfo для `DateTime(timezone=True)` при round-trip — значения,
    прочитанные напрямую из стора (в обход нормализации внутри
    `LeadStatusService`), возвращаются naive. Реальный Postgres так не делает.
    Приводим обе стороны к naive UTC перед сравнением, чтобы тест не зависел от
    этой особенности тестового SQLite-бэкенда."""
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


BACKENDS = ["memory", "postgres"]


async def _stores(backend: str):
    if backend == "memory":
        return MemoryLeadStore(), MemoryAuditStore(), MemoryOutboxStore()
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    await init_models(engine)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    return (
        PostgresLeadStore(sessionmaker=sm),
        PostgresAuditStore(sessionmaker=sm),
        PostgresOutboxStore(sessionmaker=sm),
    )


async def _service(backend: str) -> LeadStatusService:
    lead_store, audit_store, outbox_store = await _stores(backend)
    return LeadStatusService(lead_store=lead_store, audit_store=audit_store, outbox_store=outbox_store)


# ========================================================================================
# Чистые правила переходов (SOURCE_RIGHTS / BOT_TRANSITIONS / MANAGER_ONLY).
# ========================================================================================

def test_manager_only_and_bot_auto_statuses_partition_all_11():
    assert MANAGER_ONLY == {"tested_thinking", "pre_contract", "contract", "rejected", "invalid_number"}
    assert MANAGER_ONLY.issubset(LEAD_STATUSES)


def test_bot_transitions_never_target_manager_only_status():
    targets = {target for _, target in BOT_TRANSITIONS}
    assert targets.isdisjoint(MANAGER_ONLY)


def test_source_rights_covers_bot_admin_trello_system():
    assert set(SOURCE_RIGHTS) == {"bot", "admin", "trello", "system"}
    assert SOURCE_RIGHTS["bot"] == "auto"
    assert SOURCE_RIGHTS["admin"] == "manual"
    assert SOURCE_RIGHTS["trello"] == "manual"   # trello rights = admin/manual (зарезервировано)
    assert SOURCE_RIGHTS["system"] == "system"


def test_system_reuses_bot_safe_transition_table_not_unlimited():
    # system — минимальный документированный набор, НЕ безлимитный bypass.
    assert SYSTEM_TRANSITIONS == BOT_TRANSITIONS
    assert is_transition_allowed("system", "new", "in_progress") is True
    assert is_transition_allowed("system", "new", "tested_thinking") is False   # manager-only и для system


def test_is_transition_allowed_admin_and_trello_accept_any_status():
    for status in LEAD_STATUSES:
        assert is_transition_allowed("admin", "new", status) is True
        assert is_transition_allowed("trello", "new", status) is True


def test_is_transition_allowed_unknown_source_rejected():
    assert is_transition_allowed("whatsapp_widget", "new", "in_progress") is False


# ========================================================================================
# 1-4: разрешённые/запрещённые переходы бота, manager-only, admin bypass.
# ========================================================================================

@pytest.mark.parametrize("backend", BACKENDS)
def test_allowed_bot_transition_applies(backend):
    async def scenario():
        service = await _service(backend)
        lead = await service._lead_store.create_lead(lead_status="new")

        result = await service.set_status(lead.id, "in_progress", "bot")

        assert result.changed is True
        assert result.previous_status == "new"
        assert result.current_status == "in_progress"
        assert result.rejected_reason is None
        fetched = await service._lead_store.get_lead(lead.id)
        assert fetched.lead_status == "in_progress"

    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_forbidden_bot_transition_rejected(backend):
    async def scenario():
        service = await _service(backend)
        # thinking -> info_sent НЕ в BOT_TRANSITIONS (разрешено только new/in_progress->info_sent).
        lead = await service._lead_store.create_lead(lead_status="thinking")

        result = await service.set_status(lead.id, "info_sent", "bot")

        assert result.changed is False
        assert result.rejected_reason == "transition_not_allowed"
        fetched = await service._lead_store.get_lead(lead.id)
        assert fetched.lead_status == "thinking"

    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_manager_only_status_not_settable_by_bot(backend):
    async def scenario():
        service = await _service(backend)
        lead = await service._lead_store.create_lead(lead_status="new")

        result = await service.set_status(lead.id, "tested_thinking", "bot")

        assert result.changed is False
        assert result.rejected_reason == "manager_only"
        fetched = await service._lead_store.get_lead(lead.id)
        assert fetched.lead_status == "new"
        blocked = await service._audit_store.list_by_event_type("status_change_blocked")
        assert len(blocked) == 1
        assert blocked[0].reason == "manager_only"

    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_admin_can_set_manager_only_status(backend):
    async def scenario():
        service = await _service(backend)
        lead = await service._lead_store.create_lead(lead_status="new")

        result = await service.set_status(lead.id, "tested_thinking", "admin", actor="albina")

        assert result.changed is True
        assert result.current_status == "tested_thinking"
        fetched = await service._lead_store.get_lead(lead.id)
        assert fetched.lead_status == "tested_thinking"
        assert fetched.manual_status_lock_until is not None

    _run(scenario())


# ========================================================================================
# 5-7: no-op.
# ========================================================================================

@pytest.mark.parametrize("backend", BACKENDS)
def test_same_status_is_noop_without_outbox_or_audit(backend):
    async def scenario():
        service = await _service(backend)
        lead = await service._lead_store.create_lead(lead_status="new")

        result = await service.set_status(lead.id, "new", "bot")

        assert result.changed is False
        assert result.rejected_reason is None
        assert result.current_status == "new"
        outbox_events = await service._outbox_store.list_for_aggregate(lead.id)
        assert outbox_events == []
        audits = await service._audit_store.list_for_lead(lead.id)
        assert [a for a in audits if a.event_type == "lead_status_changed"] == []

    _run(scenario())


# ========================================================================================
# 8-10: manual lock.
# ========================================================================================

@pytest.mark.parametrize("backend", BACKENDS)
def test_bot_blocked_by_active_manual_lock_but_saves_suggested_status(backend):
    async def scenario():
        service = await _service(backend)
        now = _now()
        lead = await service._lead_store.create_lead(lead_status="new")
        await service._lead_store.update_lead(lead.id, manual_status_lock_until=now + timedelta(minutes=10))

        result = await service.set_status(lead.id, "in_progress", "bot", now=now, suggested_status="in_progress")

        assert result.changed is False
        assert result.rejected_reason == "manual_lock"
        fetched = await service._lead_store.get_lead(lead.id)
        assert fetched.lead_status == "new"
        assert fetched.suggested_status == "in_progress"

    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_bot_transition_allowed_after_lock_expiry(backend):
    async def scenario():
        service = await _service(backend)
        now = _now()
        lead = await service._lead_store.create_lead(lead_status="new")
        await service._lead_store.update_lead(lead.id, manual_status_lock_until=now - timedelta(minutes=1))

        result = await service.set_status(lead.id, "in_progress", "bot", now=now)

        assert result.changed is True
        assert result.current_status == "in_progress"

    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_admin_change_creates_30min_manual_lock(backend):
    async def scenario():
        service = await _service(backend)
        now = _now()
        lead = await service._lead_store.create_lead(lead_status="new")

        result = await service.set_status(lead.id, "in_progress", "admin", now=now, actor="manager1")

        assert result.changed is True
        assert result.manual_lock_until == now + timedelta(minutes=30)
        fetched = await service._lead_store.get_lead(lead.id)
        assert _naive(fetched.manual_status_lock_until) == _naive(now + timedelta(minutes=30))

    _run(scenario())


def test_system_does_not_bypass_manual_lock_without_force():
    async def scenario():
        service = await _service("memory")
        now = _now()
        lead = await service._lead_store.create_lead(lead_status="new")
        await service._lead_store.update_lead(lead.id, manual_status_lock_until=now + timedelta(minutes=10))

        blocked = await service.set_status(lead.id, "in_progress", "system", now=now)
        assert blocked.changed is False
        assert blocked.rejected_reason == "manual_lock"

        forced = await service.set_status(lead.id, "in_progress", "system", now=now, force=True)
        assert forced.changed is True
        assert forced.current_status == "in_progress"

    _run(scenario())


# ========================================================================================
# 11-13: source/reason/actor/confidence; invalid status/source.
# ========================================================================================

def test_source_reason_actor_confidence_recorded_in_audit():
    async def scenario():
        service = await _service("memory")
        lead = await service._lead_store.create_lead(lead_status="new")

        result = await service.set_status(
            lead.id, "callback", "bot", actor="ai-classifier",
            reason="клиент попросил перезвонить", confidence=0.97,
        )

        assert result.changed is True
        audits = await service._audit_store.list_for_lead(lead.id)
        changed = [a for a in audits if a.event_type == "lead_status_changed"]
        assert len(changed) == 1
        row = changed[0]
        assert row.source == "bot"
        assert row.actor == "ai-classifier"
        assert row.reason == "клиент попросил перезвонить"
        assert row.confidence == 0.97
        assert row.previous_status == "new"
        assert row.new_status == "callback"

    _run(scenario())


def test_invalid_target_status_rejected():
    async def scenario():
        service = await _service("memory")
        lead = await service._lead_store.create_lead(lead_status="new")

        result = await service.set_status(lead.id, "not_a_real_status", "bot")

        assert result.changed is False
        assert result.rejected_reason == "invalid_status"

    _run(scenario())


def test_invalid_source_rejected():
    async def scenario():
        service = await _service("memory")
        lead = await service._lead_store.create_lead(lead_status="new")

        result = await service.set_status(lead.id, "in_progress", "whatsapp_widget")

        assert result.changed is False
        assert result.rejected_reason == "invalid_source"

    _run(scenario())


# ========================================================================================
# 18-24: ConversationService (takeover/release/pause), owner audit, legacy mapping.
# ========================================================================================

def test_takeover_changes_owner_not_lead_status():
    async def scenario():
        lead_store = MemoryLeadStore()
        service = ConversationService(lead_store=lead_store, audit_store=MemoryAuditStore())
        lead = await lead_store.create_lead(lead_status="in_progress")
        conv = await lead_store.create_conversation(bot_id="college_1", external_user_id="tg-100", lead_id=lead.id)

        updated = await service.takeover(conv.id, "manager1")

        assert updated.dialog_owner == "manager"
        assert updated.assigned_to == "manager1"
        fetched_lead = await lead_store.get_lead(lead.id)
        assert fetched_lead.lead_status == "in_progress"

    _run(scenario())


def test_release_sets_owner_bot_and_keeps_assigned_to():
    async def scenario():
        lead_store = MemoryLeadStore()
        service = ConversationService(lead_store=lead_store, audit_store=MemoryAuditStore())
        conv = await lead_store.create_conversation(bot_id="college_1", external_user_id="tg-101")
        await service.takeover(conv.id, "manager1")

        released = await service.release(conv.id, actor="manager1")

        assert released.dialog_owner == "bot"
        assert released.assigned_to == "manager1"   # KEEP — не снимается

    _run(scenario())


def test_pause_sets_owner_paused_without_assigning_manager():
    async def scenario():
        lead_store = MemoryLeadStore()
        service = ConversationService(lead_store=lead_store, audit_store=MemoryAuditStore())
        conv = await lead_store.create_conversation(bot_id="college_1", external_user_id="tg-102")

        paused = await service.pause(conv.id, actor="manager2")

        assert paused.dialog_owner == "paused"
        assert paused.assigned_to == ""   # в отличие от takeover — не назначается

    _run(scenario())


def test_dialog_owner_change_creates_audit_with_previous_and_new_owner():
    async def scenario():
        lead_store = MemoryLeadStore()
        audit_store = MemoryAuditStore()
        service = ConversationService(lead_store=lead_store, audit_store=audit_store)
        conv = await lead_store.create_conversation(bot_id="college_1", external_user_id="tg-103")

        await service.takeover(conv.id, "manager3", reason="клиент попросил менеджера")

        audits = await audit_store.list_by_event_type("dialog_owner_changed")
        assert len(audits) == 1
        row = audits[0]
        assert row.previous_owner == "bot"
        assert row.new_owner == "manager"
        assert row.actor == "manager3"
        assert row.reason == "клиент попросил менеджера"
        assert row.conversation_id == conv.id

    _run(scenario())


def test_resetting_same_dialog_owner_is_noop_without_audit():
    async def scenario():
        lead_store = MemoryLeadStore()
        audit_store = MemoryAuditStore()
        service = ConversationService(lead_store=lead_store, audit_store=audit_store)
        conv = await lead_store.create_conversation(bot_id="college_1", external_user_id="tg-104")

        result = await service.release(conv.id)   # уже bot -> no-op

        assert result.dialog_owner == "bot"
        assert (await audit_store.list_by_event_type("dialog_owner_changed")) == []

    _run(scenario())


def test_dialog_owner_change_creates_no_outbox_event():
    async def scenario():
        lead_store = MemoryLeadStore()
        audit_store = MemoryAuditStore()
        outbox_store = MemoryOutboxStore()
        service = ConversationService(lead_store=lead_store, audit_store=audit_store)
        conv = await lead_store.create_conversation(bot_id="college_1", external_user_id="tg-300")

        await service.takeover(conv.id, "manager5")

        assert (await outbox_store.list_pending()) == []

    _run(scenario())


def test_legacy_intercepted_mapping_stays_consistent_via_conversation_service():
    async def scenario():
        lead_store = MemoryLeadStore()
        service = ConversationService(lead_store=lead_store, audit_store=MemoryAuditStore())
        conv = await lead_store.create_conversation(bot_id="college_1", external_user_id="tg-105")

        taken = await service.takeover(conv.id, "manager4")
        assert intercepted_to_dialog_owner(True) == "manager" == taken.dialog_owner
        assert dialog_owner_to_intercepted(taken.dialog_owner) is True

        paused = await service.pause(conv.id)
        assert isinstance(dialog_owner_to_intercepted(paused.dialog_owner), bool)

        released = await service.release(conv.id)
        assert dialog_owner_to_intercepted(released.dialog_owner) is False

    _run(scenario())


# ========================================================================================
# 25-27, 32: apply_invited_handoff — atomicity, single outbox, injected failure.
# ========================================================================================

@pytest.mark.parametrize("backend", BACKENDS)
def test_apply_invited_handoff_applies_all_three_atomically(backend):
    async def scenario():
        service = await _service(backend)
        lead = await service._lead_store.create_lead(lead_status="in_progress")
        conv = await service._lead_store.create_conversation(
            bot_id="college_1", external_user_id="tg-200", lead_id=lead.id,
        )

        result = await service.apply_invited_handoff(lead.id, conv.id, actor="bot", manager="дежурный")

        assert result.changed is True
        assert result.current_status == "invited"

        fetched_lead = await service._lead_store.get_lead(lead.id)
        fetched_conv = await service._lead_store.get_conversation(conv.id)
        assert fetched_lead.lead_status == "invited"
        assert fetched_conv.bot_phase == "handoff"
        assert fetched_conv.dialog_owner == "manager"

        outbox_events = await service._outbox_store.list_for_aggregate(lead.id)
        assert len(outbox_events) == 1
        assert outbox_events[0].event_type == "lead_status_changed"

    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_apply_invited_handoff_injected_failure_applies_nothing(backend):
    async def scenario():
        service = await _service(backend)
        lead = await service._lead_store.create_lead(lead_status="in_progress")
        conv = await service._lead_store.create_conversation(
            bot_id="college_1", external_user_id="tg-201", lead_id=lead.id,
        )

        with pytest.raises(RuntimeError):
            await service.apply_invited_handoff(lead.id, conv.id, actor="bot", _raise_before_commit=True)

        fetched_lead = await service._lead_store.get_lead(lead.id)
        fetched_conv = await service._lead_store.get_conversation(conv.id)
        assert fetched_lead.lead_status == "in_progress"     # не изменился
        assert fetched_conv.bot_phase == "greeting"           # не изменился
        assert fetched_conv.dialog_owner == "bot"              # не изменился
        assert (await service._outbox_store.list_for_aggregate(lead.id)) == []
        audits = await service._audit_store.list_for_lead(lead.id)
        assert [a for a in audits if a.event_type == "lead_status_changed"] == []

    _run(scenario())


# ========================================================================================
# 28-29: реальная смена создаёт audit+outbox; смена dialog_owner — НЕ создаёт outbox
# (последнее уже покрыто test_dialog_owner_change_creates_no_outbox_event выше).
# ========================================================================================

@pytest.mark.parametrize("backend", BACKENDS)
def test_real_status_change_creates_audit_and_outbox(backend):
    async def scenario():
        service = await _service(backend)
        lead = await service._lead_store.create_lead(lead_status="new")

        result = await service.set_status(lead.id, "in_progress", "bot")

        assert result.changed is True
        assert result.outbox_event_id is not None

        audits = await service._audit_store.list_for_lead(lead.id)
        changed = [a for a in audits if a.event_type == "lead_status_changed"]
        assert len(changed) == 1

        outbox_events = await service._outbox_store.list_for_aggregate(lead.id)
        assert len(outbox_events) == 1
        assert outbox_events[0].status == "pending"
        assert outbox_events[0].idempotency_key == (
            f"lead:{lead.id}:status:in_progress:change:{changed[0].id}"
        )

    _run(scenario())


# ========================================================================================
# 30: idempotency_key уникален (оба бэкенда).
# ========================================================================================

@pytest.mark.parametrize("backend", BACKENDS)
def test_outbox_idempotency_key_unique(backend):
    async def scenario():
        _, _, outbox_store = await _stores(backend)
        await outbox_store.create(aggregate_id=1, idempotency_key="lead:1:status:in_progress:change:1")
        with pytest.raises(Exception):
            await outbox_store.create(aggregate_id=1, idempotency_key="lead:1:status:in_progress:change:1")

    _run(scenario())


# ========================================================================================
# 32: сбой на пути записи outbox откатывает всё (без частичного изменения).
# ========================================================================================

@pytest.mark.parametrize("backend", BACKENDS)
def test_set_status_injected_failure_leaves_nothing_partial(backend):
    async def scenario():
        service = await _service(backend)
        lead = await service._lead_store.create_lead(lead_status="new")

        with pytest.raises(RuntimeError):
            await service.set_status(lead.id, "in_progress", "bot", _raise_before_commit=True)

        fetched = await service._lead_store.get_lead(lead.id)
        assert fetched.lead_status == "new"
        assert (await service._outbox_store.list_for_aggregate(lead.id)) == []
        audits = await service._audit_store.list_for_lead(lead.id)
        assert [a for a in audits if a.event_type == "lead_status_changed"] == []

    _run(scenario())


# ========================================================================================
# 31: memory/PG контракт идентичен — уже покрыто параметризацией BACKENDS выше на
# ключевых сценариях (allowed/forbidden/manager-only/no-op/lock/handoff/idempotency/
# injected-failure). Дополнительная явная проверка round-trip для наглядности:
# ========================================================================================

@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_and_postgres_lead_status_service_share_identical_contract(backend):
    async def scenario():
        service = await _service(backend)
        lead = await service._lead_store.create_lead(lead_status="new")

        r1 = await service.set_status(lead.id, "in_progress", "bot")
        r2 = await service.set_status(lead.id, "in_progress", "bot")   # no-op второй раз

        assert r1.changed is True
        assert r2.changed is False
        assert r2.rejected_reason is None

    _run(scenario())

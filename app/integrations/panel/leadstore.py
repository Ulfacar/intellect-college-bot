"""Канонические Conversation/Lead сторы (Increment 2 телеграм-пилота).

Это НОВЫЙ, минимально необходимый слой поверх новых таблиц `pilot_conversations` и
`leads` (см. `app/integrations/crm/db.py::PilotConversation/Lead`). Он НЕ заменяет и не
переписывает `app/integrations/panel/store.py` (legacy `ConversationView`/`Conversation`,
таблица `conversations`) — старая панель продолжает работать как есть.

Ключевые инварианты (см. `docs/phase1-implementation-plan.md`,
`docs/admin-bot-control-and-ai-classification-spec.md`):
- Conversation и Lead — раздельные сущности; связь `conversation.lead_id -> lead.id`.
- Lead может существовать без Conversation; Conversation может временно быть без Lead.
- `lead_status` канонически хранится ТОЛЬКО в Lead/PostgreSQL — не дублируется в
  `DialogState` (app/core/state.py) и не зеркалится в legacy `stage`.
- `stage`/`intercepted` (legacy) не трогаются; ниже — только read-only helpers для
  сопоставления `intercepted <-> dialog_owner`, не подключённые к оркестратору/перехвату
  (это Increment 3).

Два бэкенда за одним контрактом (по образцу `panel/store.py`):
- `MemoryLeadStore` — дефолт (тесты, офлайн);
- `PostgresLeadStore` — прод, реюз sessionmaker из `crm/db.py`.
Выбор — `settings.panel_backend` (та же настройка, что и у `get_conversation_store()`);
`get_lead_store()` отдаёт нужный singleton.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings

# --------------------------------------------------------------------------------------
# Допустимые значения перечислений (см. ТЗ Increment 2).
# --------------------------------------------------------------------------------------

BOT_PHASES = {"greeting", "qualification", "consultation", "waiting", "handoff"}
DIALOG_OWNERS = {"bot", "manager", "paused"}
LEAD_STATUSES = {
    "new", "pre_contract", "in_progress", "contract", "tested_thinking", "callback",
    "thinking", "invited", "info_sent", "rejected", "invalid_number",
}

DEFAULT_BOT_PHASE = "greeting"
DEFAULT_DIALOG_OWNER = "bot"
DEFAULT_LEAD_STATUS = "new"
DEFAULT_LEAD_SOURCE = "telegram_test"       # единственный источник для телеграм-пилота
DEFAULT_LEAD_TEMPERATURE = "new"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_session_id() -> str:
    return uuid.uuid4().hex


def _validate(value: str, allowed: set[str], field_name: str) -> None:
    if value not in allowed:
        raise ValueError(f"недопустимое значение {field_name}={value!r}, ожидается одно из {sorted(allowed)}")


# --------------------------------------------------------------------------------------
# UNSET-сентинел (Increment 3): для "очищаемых" полей update_lead/update_conversation.
# omitted (UNSET, дефолт параметра) -> не менять; None -> очистить (только nullable
# поля); значение -> установить. Non-nullable поля отклоняют явный None (ValueError).
# --------------------------------------------------------------------------------------

class _UnsetType:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - только для отладочного вывода
        return "UNSET"

    def __bool__(self) -> bool:
        # UNSET не должен случайно восприниматься как falsy-значение поля.
        raise TypeError("UNSET не имеет истинностного значения — сравнивайте через `is UNSET`")


UNSET: Any = _UnsetType()


class ConflictError(Exception):
    """Нарушение active-session guard: попытка создать вторую активную (не
    заархивированную) `pilot_conversations`-сессию для того же `(bot_id,
    external_user_id)`. См. `create_conversation`/`create_new_session` ниже и
    комментарий про partial unique index в `migrations/0002_lead_status_audit_outbox.sql`
    (Increment 4)."""


_LEAD_NON_NULLABLE_FIELDS = {"lead_status", "lead_source", "name", "lead_temperature", "qualification"}
_LEAD_NULLABLE_FIELDS = {
    "phone", "telegram_username", "grade_base", "direction", "suggested_status",
    "next_action_type", "next_action_at", "ai_summary", "escalation_reason",
    "manual_status_lock_until", "status_change_source", "status_change_by", "status_change_reason",
}


def _resolve_lead_updates(**fields: Any) -> dict[str, Any]:
    """UNSET-семантика для `Lead`. Возвращает только реально изменяемые поля."""
    updates: dict[str, Any] = {}
    for key, value in fields.items():
        if value is UNSET:
            continue
        if value is None:
            if key in _LEAD_NON_NULLABLE_FIELDS:
                raise ValueError(f"{key} не может быть очищен через None (non-nullable поле Lead)")
            updates[key] = None
            continue
        if key == "lead_status":
            _validate(value, LEAD_STATUSES, "lead_status")
        updates[key] = value
    return updates


_CONV_NON_NULLABLE_FIELDS = {"bot_phase", "dialog_owner"}


def _resolve_conversation_updates(**fields: Any) -> dict[str, Any]:
    """UNSET-семантика для `PilotConversation`. `assigned_to` в схеме NOT NULL
    (default '') — явный None для него трактуется как "очистить" -> пустая строка
    (нет физического NULL-состояния, но семантически то же самое: снять ответственного).
    """
    updates: dict[str, Any] = {}
    for key, value in fields.items():
        if value is UNSET:
            continue
        if value is None:
            if key in _CONV_NON_NULLABLE_FIELDS:
                raise ValueError(f"{key} не может быть очищен через None (non-nullable поле PilotConversation)")
            updates[key] = "" if key == "assigned_to" else None
            continue
        if key == "bot_phase":
            _validate(value, BOT_PHASES, "bot_phase")
        if key == "dialog_owner":
            _validate(value, DIALOG_OWNERS, "dialog_owner")
        updates[key] = value
    return updates


# --------------------------------------------------------------------------------------
# Legacy-совместимость: intercepted(bool) <-> dialog_owner(enum). Только helpers для
# чтения/документации — НЕ подключены к orchestrator/takeover в этом инкременте.
# --------------------------------------------------------------------------------------

def intercepted_to_dialog_owner(intercepted: bool) -> str:
    """Legacy bool -> новый enum. Канонический двусторонний маппинг:
    `intercepted=True` <-> `dialog_owner=manager`; `intercepted=False` <-> `dialog_owner=bot`.
    """
    return "manager" if intercepted else "bot"


def dialog_owner_to_intercepted(dialog_owner: str) -> bool:
    """Новый enum -> legacy bool. `manager` -> True, `bot` -> False.

    `paused` НЕ имеет legacy-эквивалента: старое поле `intercepted` умеет различать
    только «бот отвечает / бот молчит», но не «молчит, потому что перехвачен менеджером»
    vs «молчит, потому что на паузе, без ответственного». Мы намеренно (и с потерей
    нюанса «назначен ли менеджер») мапим `paused -> True`, т.к. фактическое использование
    `intercepted` в текущем коде — это гейт «должен ли бот отвечать», и в состоянии
    `paused` бот тоже не должен отвечать. Не считать это авторитетным источником для
    `assigned_to`/владения диалогом — только приближение для обратной совместимости.
    """
    return dialog_owner in ("manager", "paused")


# --------------------------------------------------------------------------------------
# Легковесные view-dataclass'ы (UI/тесты не зависят от ORM).
# --------------------------------------------------------------------------------------

@dataclass
class LeadView:
    id: int
    lead_status: str = DEFAULT_LEAD_STATUS
    lead_source: str = DEFAULT_LEAD_SOURCE
    name: str = ""
    phone: str | None = None
    telegram_username: str | None = None
    grade_base: str | None = None
    direction: str | None = None
    qualification: dict[str, Any] = field(default_factory=dict)
    lead_temperature: str = DEFAULT_LEAD_TEMPERATURE
    suggested_status: str | None = None
    next_action_type: str | None = None
    next_action_at: datetime | None = None
    ai_summary: str | None = None
    escalation_reason: str | None = None
    manual_status_lock_until: datetime | None = None
    status_change_source: str | None = None
    status_change_by: str | None = None
    status_change_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class PilotConversationView:
    id: int
    session_id: str = ""
    channel: str = "telegram"
    bot_id: str = ""
    external_user_id: str = ""
    external_chat_id: str = ""
    lead_id: int | None = None
    bot_phase: str = DEFAULT_BOT_PHASE
    dialog_owner: str = DEFAULT_DIALOG_OWNER
    assigned_to: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    archived_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        """Активна = не заархивирована (`archived_at is None`)."""
        return self.archived_at is None


# --------------------------------------------------------------------------------------
# Memory backend
# --------------------------------------------------------------------------------------

class MemoryLeadStore:
    """Conversation/Lead сторы в памяти процесса (дефолт: тесты, офлайн-демо)."""

    def __init__(self) -> None:
        self._leads: dict[int, LeadView] = {}
        self._lead_seq = 0
        self._conversations: dict[int, PilotConversationView] = {}
        self._conv_seq = 0

    # ---- Lead ----

    async def create_lead(
        self, *, lead_status: str = DEFAULT_LEAD_STATUS, lead_source: str = DEFAULT_LEAD_SOURCE,
        name: str = "", phone: str | None = None, telegram_username: str | None = None,
        grade_base: str | None = None, direction: str | None = None,
        qualification: dict[str, Any] | None = None,
        lead_temperature: str = DEFAULT_LEAD_TEMPERATURE,
    ) -> LeadView:
        _validate(lead_status, LEAD_STATUSES, "lead_status")
        self._lead_seq += 1
        now = _now()
        lead = LeadView(
            id=self._lead_seq, lead_status=lead_status, lead_source=lead_source, name=name,
            phone=phone, telegram_username=telegram_username, grade_base=grade_base,
            direction=direction, qualification=dict(qualification or {}),
            lead_temperature=lead_temperature, created_at=now, updated_at=now,
        )
        self._leads[lead.id] = lead
        return lead

    async def get_lead(self, lead_id: int) -> LeadView | None:
        return self._leads.get(lead_id)

    async def update_lead(
        self, lead_id: int, *, lead_status: Any = UNSET, lead_source: Any = UNSET, name: Any = UNSET,
        phone: Any = UNSET, telegram_username: Any = UNSET, grade_base: Any = UNSET, direction: Any = UNSET,
        qualification: Any = UNSET, lead_temperature: Any = UNSET, suggested_status: Any = UNSET,
        next_action_type: Any = UNSET, next_action_at: Any = UNSET, ai_summary: Any = UNSET,
        escalation_reason: Any = UNSET, manual_status_lock_until: Any = UNSET,
        status_change_source: Any = UNSET, status_change_by: Any = UNSET, status_change_reason: Any = UNSET,
    ) -> LeadView | None:
        lead = self._leads.get(lead_id)
        if lead is None:
            return None
        updates = _resolve_lead_updates(
            lead_status=lead_status, lead_source=lead_source, name=name, phone=phone,
            telegram_username=telegram_username, grade_base=grade_base, direction=direction,
            qualification=qualification, lead_temperature=lead_temperature,
            suggested_status=suggested_status, next_action_type=next_action_type,
            next_action_at=next_action_at, ai_summary=ai_summary, escalation_reason=escalation_reason,
            manual_status_lock_until=manual_status_lock_until, status_change_source=status_change_source,
            status_change_by=status_change_by, status_change_reason=status_change_reason,
        )
        for key, value in updates.items():
            setattr(lead, key, value)
        lead.updated_at = _now()
        return lead

    # ---- Conversation ----

    async def create_conversation(
        self, *, session_id: str | None = None, channel: str = "telegram", bot_id: str = "",
        external_user_id: str = "", external_chat_id: str = "", lead_id: int | None = None,
        bot_phase: str = DEFAULT_BOT_PHASE, dialog_owner: str = DEFAULT_DIALOG_OWNER,
    ) -> PilotConversationView:
        _validate(bot_phase, BOT_PHASES, "bot_phase")
        _validate(dialog_owner, DIALOG_OWNERS, "dialog_owner")
        if bot_id and external_user_id:
            existing = await self.get_active_conversation(bot_id, external_user_id)
            if existing is not None:
                raise ConflictError(
                    f"активная сессия уже существует для bot_id={bot_id!r} "
                    f"external_user_id={external_user_id!r} (conversation_id={existing.id})"
                )
        self._conv_seq += 1
        now = _now()
        conv = PilotConversationView(
            id=self._conv_seq, session_id=session_id or _new_session_id(), channel=channel,
            bot_id=bot_id, external_user_id=external_user_id, external_chat_id=external_chat_id,
            lead_id=lead_id, bot_phase=bot_phase, dialog_owner=dialog_owner,
            created_at=now, updated_at=now,
        )
        self._conversations[conv.id] = conv
        return conv

    async def get_conversation(self, conversation_id: int) -> PilotConversationView | None:
        return self._conversations.get(conversation_id)

    async def get_active_conversation(self, bot_id: str, external_user_id: str) -> PilotConversationView | None:
        """Активная = не заархивированная (`archived_at is None`) сессия по `bot_id`+юзеру."""
        candidates = [
            c for c in self._conversations.values()
            if c.bot_id == bot_id and c.external_user_id == external_user_id and c.is_active
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda c: c.created_at or _now(), reverse=True)
        return candidates[0]

    async def link_conversation_to_lead(self, conversation_id: int, lead_id: int) -> None:
        conv = self._conversations.get(conversation_id)
        if conv is not None:
            conv.lead_id = lead_id
            conv.updated_at = _now()

    async def archive_conversation(self, conversation_id: int) -> None:
        conv = self._conversations.get(conversation_id)
        if conv is not None:
            conv.archived_at = _now()
            conv.updated_at = _now()

    async def update_conversation(
        self, conversation_id: int, *, bot_phase: Any = UNSET, dialog_owner: Any = UNSET,
        assigned_to: Any = UNSET, lead_id: Any = UNSET, archived_at: Any = UNSET,
    ) -> PilotConversationView | None:
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return None
        updates = _resolve_conversation_updates(
            bot_phase=bot_phase, dialog_owner=dialog_owner, assigned_to=assigned_to,
            lead_id=lead_id, archived_at=archived_at,
        )
        for key, value in updates.items():
            setattr(conv, key, value)
        conv.updated_at = _now()
        return conv

    async def create_new_session(
        self, *, bot_id: str, external_user_id: str, external_chat_id: str = "",
        channel: str = "telegram", lead_source: str = DEFAULT_LEAD_SOURCE,
    ) -> tuple[PilotConversationView, LeadView]:
        """Начать новую сессию: архивировать текущую активную (история НЕ удаляется),
        создать свежий Conversation + свежий Lead и связать их."""
        old = await self.get_active_conversation(bot_id, external_user_id)
        if old is not None:
            await self.archive_conversation(old.id)
        lead = await self.create_lead(lead_source=lead_source)
        conv = await self.create_conversation(
            channel=channel, bot_id=bot_id, external_user_id=external_user_id,
            external_chat_id=external_chat_id, lead_id=lead.id,
        )
        return conv, lead


# --------------------------------------------------------------------------------------
# Postgres backend
# --------------------------------------------------------------------------------------

class PostgresLeadStore:
    """Conversation/Lead сторы в Postgres (прод). sessionmaker инъектируется в тестах."""

    def __init__(self, sessionmaker: async_sessionmaker | None = None) -> None:
        self._sessionmaker = sessionmaker

    def _sm(self) -> async_sessionmaker:
        if self._sessionmaker is None:
            from app.integrations.crm.db import get_sessionmaker
            self._sessionmaker = get_sessionmaker()
        return self._sessionmaker

    def sessionmaker(self) -> async_sessionmaker:
        """Публичный доступ к sessionmaker — используется `LeadStatusService` для
        однотранзакционных атомарных записей (lead+audit+outbox), которые не могут
        идти через отдельно коммитящие методы этого стора (см.
        `app/core/lead_status_service.py::_apply_atomic_postgres`)."""
        return self._sm()

    # ---- Lead ----

    async def create_lead(
        self, *, lead_status: str = DEFAULT_LEAD_STATUS, lead_source: str = DEFAULT_LEAD_SOURCE,
        name: str = "", phone: str | None = None, telegram_username: str | None = None,
        grade_base: str | None = None, direction: str | None = None,
        qualification: dict[str, Any] | None = None,
        lead_temperature: str = DEFAULT_LEAD_TEMPERATURE,
    ) -> LeadView:
        _validate(lead_status, LEAD_STATUSES, "lead_status")
        from app.integrations.crm.db import Lead
        async with self._sm()() as session:
            row = Lead(
                lead_status=lead_status, lead_source=lead_source, name=name, phone=phone,
                telegram_username=telegram_username, grade_base=grade_base, direction=direction,
                qualification=dict(qualification or {}), lead_temperature=lead_temperature,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _lead_view(row)

    async def get_lead(self, lead_id: int) -> LeadView | None:
        from app.integrations.crm.db import Lead
        async with self._sm()() as session:
            row = await session.get(Lead, lead_id)
            return _lead_view(row) if row is not None else None

    async def update_lead(
        self, lead_id: int, *, lead_status: Any = UNSET, lead_source: Any = UNSET, name: Any = UNSET,
        phone: Any = UNSET, telegram_username: Any = UNSET, grade_base: Any = UNSET, direction: Any = UNSET,
        qualification: Any = UNSET, lead_temperature: Any = UNSET, suggested_status: Any = UNSET,
        next_action_type: Any = UNSET, next_action_at: Any = UNSET, ai_summary: Any = UNSET,
        escalation_reason: Any = UNSET, manual_status_lock_until: Any = UNSET,
        status_change_source: Any = UNSET, status_change_by: Any = UNSET, status_change_reason: Any = UNSET,
    ) -> LeadView | None:
        updates = _resolve_lead_updates(
            lead_status=lead_status, lead_source=lead_source, name=name, phone=phone,
            telegram_username=telegram_username, grade_base=grade_base, direction=direction,
            qualification=qualification, lead_temperature=lead_temperature,
            suggested_status=suggested_status, next_action_type=next_action_type,
            next_action_at=next_action_at, ai_summary=ai_summary, escalation_reason=escalation_reason,
            manual_status_lock_until=manual_status_lock_until, status_change_source=status_change_source,
            status_change_by=status_change_by, status_change_reason=status_change_reason,
        )
        from app.integrations.crm.db import Lead
        async with self._sm()() as session:
            row = await session.get(Lead, lead_id)
            if row is None:
                return None
            for key, value in updates.items():
                setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
            return _lead_view(row)

    # ---- Conversation ----

    async def create_conversation(
        self, *, session_id: str | None = None, channel: str = "telegram", bot_id: str = "",
        external_user_id: str = "", external_chat_id: str = "", lead_id: int | None = None,
        bot_phase: str = DEFAULT_BOT_PHASE, dialog_owner: str = DEFAULT_DIALOG_OWNER,
    ) -> PilotConversationView:
        _validate(bot_phase, BOT_PHASES, "bot_phase")
        _validate(dialog_owner, DIALOG_OWNERS, "dialog_owner")
        if bot_id and external_user_id:
            existing = await self.get_active_conversation(bot_id, external_user_id)
            if existing is not None:
                raise ConflictError(
                    f"активная сессия уже существует для bot_id={bot_id!r} "
                    f"external_user_id={external_user_id!r} (conversation_id={existing.id})"
                )
        from app.integrations.crm.db import PilotConversation
        async with self._sm()() as session:
            row = PilotConversation(
                session_id=session_id or _new_session_id(), channel=channel, bot_id=bot_id,
                external_user_id=external_user_id, external_chat_id=external_chat_id,
                lead_id=lead_id, bot_phase=bot_phase, dialog_owner=dialog_owner,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _conv_view(row)

    async def get_conversation(self, conversation_id: int) -> PilotConversationView | None:
        from app.integrations.crm.db import PilotConversation
        async with self._sm()() as session:
            row = await session.get(PilotConversation, conversation_id)
            return _conv_view(row) if row is not None else None

    async def get_active_conversation(self, bot_id: str, external_user_id: str) -> PilotConversationView | None:
        from app.integrations.crm.db import PilotConversation
        async with self._sm()() as session:
            row = (await session.execute(
                select(PilotConversation)
                .where(PilotConversation.bot_id == bot_id)
                .where(PilotConversation.external_user_id == external_user_id)
                .where(PilotConversation.archived_at.is_(None))
                .order_by(PilotConversation.created_at.desc())
                .limit(1)
            )).scalar_one_or_none()
            return _conv_view(row) if row is not None else None

    async def link_conversation_to_lead(self, conversation_id: int, lead_id: int) -> None:
        from app.integrations.crm.db import PilotConversation
        async with self._sm()() as session:
            row = await session.get(PilotConversation, conversation_id)
            if row is not None:
                row.lead_id = lead_id
                await session.commit()

    async def archive_conversation(self, conversation_id: int) -> None:
        from app.integrations.crm.db import PilotConversation
        async with self._sm()() as session:
            row = await session.get(PilotConversation, conversation_id)
            if row is not None:
                row.archived_at = _now()
                await session.commit()

    async def update_conversation(
        self, conversation_id: int, *, bot_phase: Any = UNSET, dialog_owner: Any = UNSET,
        assigned_to: Any = UNSET, lead_id: Any = UNSET, archived_at: Any = UNSET,
    ) -> PilotConversationView | None:
        updates = _resolve_conversation_updates(
            bot_phase=bot_phase, dialog_owner=dialog_owner, assigned_to=assigned_to,
            lead_id=lead_id, archived_at=archived_at,
        )
        from app.integrations.crm.db import PilotConversation
        async with self._sm()() as session:
            row = await session.get(PilotConversation, conversation_id)
            if row is None:
                return None
            for key, value in updates.items():
                setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
            return _conv_view(row)

    async def create_new_session(
        self, *, bot_id: str, external_user_id: str, external_chat_id: str = "",
        channel: str = "telegram", lead_source: str = DEFAULT_LEAD_SOURCE,
    ) -> tuple[PilotConversationView, LeadView]:
        old = await self.get_active_conversation(bot_id, external_user_id)
        if old is not None:
            await self.archive_conversation(old.id)
        lead = await self.create_lead(lead_source=lead_source)
        conv = await self.create_conversation(
            channel=channel, bot_id=bot_id, external_user_id=external_user_id,
            external_chat_id=external_chat_id, lead_id=lead.id,
        )
        return conv, lead


def _lead_view(row) -> LeadView:
    return LeadView(
        id=row.id, lead_status=row.lead_status, lead_source=row.lead_source, name=row.name or "",
        phone=row.phone, telegram_username=row.telegram_username, grade_base=row.grade_base,
        direction=row.direction, qualification=dict(row.qualification or {}),
        lead_temperature=row.lead_temperature, suggested_status=row.suggested_status,
        next_action_type=row.next_action_type, next_action_at=row.next_action_at,
        ai_summary=row.ai_summary, escalation_reason=row.escalation_reason,
        manual_status_lock_until=row.manual_status_lock_until,
        status_change_source=row.status_change_source, status_change_by=row.status_change_by,
        status_change_reason=row.status_change_reason,
        created_at=row.created_at, updated_at=row.updated_at,
    )


def _conv_view(row) -> PilotConversationView:
    return PilotConversationView(
        id=row.id, session_id=row.session_id, channel=row.channel, bot_id=row.bot_id,
        external_user_id=row.external_user_id, external_chat_id=row.external_chat_id,
        lead_id=row.lead_id, bot_phase=row.bot_phase, dialog_owner=row.dialog_owner,
        assigned_to=row.assigned_to or "", created_at=row.created_at, updated_at=row.updated_at,
        archived_at=row.archived_at,
    )


_memory_lead_store = MemoryLeadStore()
_pg_lead_store: PostgresLeadStore | None = None


def get_lead_store():
    """Сконфигурированный бэкенд (singleton), тот же переключатель, что у панели диалогов
    (`settings.panel_backend`). По умолчанию — in-memory."""
    global _pg_lead_store
    if settings.panel_backend == "postgres":
        if _pg_lead_store is None:
            _pg_lead_store = PostgresLeadStore()
        return _pg_lead_store
    return _memory_lead_store

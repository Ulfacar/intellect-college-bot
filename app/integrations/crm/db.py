"""Слой БД для сделок бота (SQLAlchemy async).

Это НЕ замена Bitrix24 как источника правды по клиенту — это собственная
персистентная запись бота: переживает рестарт (в отличие от CrmStub в памяти)
и служит фундаментом под аналитику ИИ (конверсия ИИ vs менеджер — «не Bitrix-зона»).

Движок/сессии создаются лениво из `settings.database_url`. Для тестов sessionmaker
можно подменить (SQLite в памяти) — сетевой Postgres не требуется.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func, inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import settings


class Base(DeclarativeBase):
    pass


class Deal(Base):
    """Сделка/лид, как её видит бот (зеркало действий в воронке)."""

    __tablename__ = "deals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    funnel: Mapped[str] = mapped_column(String(32))           # admission
    stage: Mapped[str] = mapped_column(String(64), default="new")
    contact: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)       # квалификация
    notes: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Conversation(Base):
    """Диалог бот↔клиент — карточка на канбан-доске админ-панели.

    Создаётся при первом входящем сообщении (ещё до сделки). `stage`/`funnel` —
    позиция в воронке (колонка доски), `intercepted` — перехвачен ли менеджером.
    """

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # user_id — КЛЮЧ диалога вида "<bot_id>:<номер>": один номер у разных ботов = разные
    # диалоги (перехват/состояние/карточка раздельные). Телефон для показа — в `phone`.
    user_id: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    phone: Mapped[str] = mapped_column(String(64), default="")      # номер клиента для отображения
    channel: Mapped[str] = mapped_column(String(32), default="")
    chat_id: Mapped[str] = mapped_column(String(128), default="")  # адрес ответа (Bitrix DIALOG_ID ≠ user_id)
    bot_id: Mapped[str] = mapped_column(String(64), default="")
    funnel: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    stage: Mapped[str] = mapped_column(String(64), default="greeting")
    intercepted: Mapped[bool] = mapped_column(default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    qualification: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ai_summary: Mapped[str] = mapped_column(Text, default="")
    manager_next_step: Mapped[str] = mapped_column(Text, default="")
    escalation_reason: Mapped[str] = mapped_column(Text, default="")
    lead_temperature: Mapped[str] = mapped_column(String(16), default="new")
    assigned_to: Mapped[str] = mapped_column(String(64), default="")   # логин менеджера, ведущего диалог
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str] = mapped_column(String(24), default="")       # in_progress|office|manager|won|lost
    last_text: Mapped[str] = mapped_column(Text, default="")  # превью последней реплики для карточки
    last_sender: Mapped[str] = mapped_column(String(16), default="")  # client|bot|manager — для сигналов
    followup_sent: Mapped[bool] = mapped_column(default=False)  # автодожим уже отправлен (один раз)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    messages: Mapped[list["ConvMessage"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="ConvMessage.id"
    )


class ConvMessage(Base):
    """Одно сообщение в диалоге (для чат-окна панели — чистый человекочитаемый лог).

    Для исходящих (bot|manager) дополнительно трекаем доставку: status переходит
    pending→sent→delivered/failed; provider_msg_id — id сообщения у Wappi (для сверки
    с delivery-status вебхуками); idempotency_key защищает от двойной отправки.
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    sender: Mapped[str] = mapped_column(String(16))          # client | bot | manager
    text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="")       # "" (входящее) | pending|sent|delivered|failed
    provider_msg_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class AuditLog(Base):
    """Журнал действий менеджеров (кто/что/когда над каким диалогом) — подотчётность."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    manager: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(32))          # login|takeover|release|send|outcome|resend
    user_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AppFlag(Base):
    """Рантайм-флаги фич (вкл/выкл из админки), переживающие рестарт."""

    __tablename__ = "app_flags"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class FaqEntry(Base):
    """Deterministic FAQ rule managed from the admin panel."""

    __tablename__ = "faq_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    funnel: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    enabled: Mapped[bool] = mapped_column(default=True, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)
    title: Mapped[str] = mapped_column(String(160), default="")
    patterns: Mapped[list[str]] = mapped_column(JSON, default=list)
    negative_terms: Mapped[list[str]] = mapped_column(JSON, default=list)
    answer: Mapped[str] = mapped_column(Text, default="")
    handoff_only: Mapped[bool] = mapped_column(default=False)
    allow_during_qualification: Mapped[bool] = mapped_column(default=True)
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Lead(Base):
    """Канонический бизнес-лид (Increment 2 телеграм-пилота).

    Единственный источник истины для `lead_status` (11 ключей воронки) — см.
    `docs/phase1-implementation-plan.md` §0.1 и `docs/admin-bot-control-and-ai-classification-spec.md`
    §5/§6. `DialogState` (app/core/state.py) НЕ является вторым источником истины и
    `lead_status` не хранит. Lead может существовать БЕЗ Conversation (например, карточка
    заведена вручную) — связь односторонняя: `PilotConversation.lead_id -> Lead.id`.

    Таблица `leads` — НОВАЯ, не пересекается с legacy `Conversation`/`conversations`
    (`panel/store.py::ConversationView`), которая остаётся как есть для старой панели.
    """

    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    lead_source: Mapped[str] = mapped_column(String(32), default="telegram_test", index=True)
    name: Mapped[str] = mapped_column(String(160), default="")
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    grade_base: Mapped[str | None] = mapped_column(String(32), nullable=True)
    direction: Mapped[str | None] = mapped_column(String(160), nullable=True)
    qualification: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    lead_temperature: Mapped[str] = mapped_column(String(16), default="new")
    suggested_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    next_action_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    escalation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_status_lock_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status_change_source: Mapped[str | None] = mapped_column(String(16), nullable=True)  # bot|admin|trello|system
    status_change_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status_change_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PilotConversation(Base):
    """Канонический диалог (Increment 2 телеграм-пилота) — `bot_phase`/`dialog_owner` живут
    здесь, `lead_status` НЕ здесь (он в `Lead`, см. выше).

    Отдельная таблица `pilot_conversations`, т.к. имя `conversations` уже занято legacy
    `Conversation` (см. класс выше) — не переименовываем и не трогаем старую панель.
    Связь `lead_id -> leads.id` nullable: диалог может временно существовать без лида,
    лид — без диалога (Q2 admin-bot-control-and-ai-classification-spec.md).
    """

    __tablename__ = "pilot_conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    channel: Mapped[str] = mapped_column(String(32), default="telegram")
    bot_id: Mapped[str] = mapped_column(String(64), default="")
    external_user_id: Mapped[str] = mapped_column(String(160), default="")
    external_chat_id: Mapped[str] = mapped_column(String(160), default="")
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True)
    bot_phase: Mapped[str] = mapped_column(String(32), default="greeting")   # greeting|qualification|consultation|waiting|handoff
    dialog_owner: Mapped[str] = mapped_column(String(16), default="bot")     # bot|manager|paused
    assigned_to: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # NULL = активна

    __table_args__ = (
        Index("ix_pilot_conversations_bot_user", "bot_id", "external_user_id"),
        Index("ix_pilot_conversations_lead_id", "lead_id"),
    )


class LeadAudit(Base):
    """Аудит смен `lead_status`/`dialog_owner`/`bot_phase` (Increment 3 телеграм-пилота).

    Additive-таблица `lead_audit`, не пересекается ни с legacy `audit_log`
    (`panel/store.py`-аудит перехватов/отправок), ни с `leads`/`pilot_conversations`
    (только ссылается на них). Единственный источник записи — `app/core/
    lead_status_service.py::LeadStatusService` (для `lead_status_changed`/
    `status_change_blocked`) и `app/core/conversation_service.py::ConversationService`
    (для `dialog_owner_changed`). Никаких секретов/полных промптов/лишних PII — только
    коды статусов/owner, короткие reason/actor.
    """

    __tablename__ = "lead_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True, index=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("pilot_conversations.id"), nullable=True)
    # lead_status_changed | dialog_owner_changed | bot_phase_changed | status_change_blocked
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    previous_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    previous_owner: Mapped[str | None] = mapped_column(String(16), nullable=True)
    new_owner: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="")   # bot|admin|trello|system
    actor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # атрибут "metadata" зарезервирован DeclarativeBase (Base.metadata) -> используем
    # metadata_ как имя python-атрибута, колонка в БД по-прежнему называется "metadata".
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Индексы на lead_id/event_type создаются через column-level index=True выше
    # (ix_lead_audit_lead_id / ix_lead_audit_event_type) — отдельных Index() в
    # __table_args__ не нужно (дублирование имени ломает create_all на SQLite).


class Outbox(Base):
    """Outbox-заглушка (Increment 3 телеграм-пилота) — событие ставится ТОЛЬКО при
    реальной смене `lead_status` (см. `LeadStatusService.set_status`/
    `apply_invited_handoff`). Никакого реального Trello-воркера в Phase 1 — события
    остаются `pending`, пока не появится Phase 2 consumer (см.
    docs/phase1-implementation-plan.md §8). Приложение и тесты никогда не делают
    сетевых вызовов через эту таблицу.
    """

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    aggregate_type: Mapped[str] = mapped_column(String(32), default="lead")
    aggregate_id: Mapped[int] = mapped_column(Integer, index=True)
    event_type: Mapped[str] = mapped_column(String(32), default="lead_status_changed")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)   # pending|processed|error
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class FaqKbEntry(Base):
    """Managed multilingual FAQ / knowledge-base entry (Increment 5 телеграм-пилота).

    NEW, additive table — does NOT collide with the legacy `faq_entries`
    (`FaqEntry` above, `app/core/faq.py`) which stays exactly as-is. See
    `docs/faq-knowledge-base-spec.md` and `app/core/faq_kb.py` for the full
    publication-lifecycle contract (draft/published/archived, versions, rollback).

    Content fields on THIS row (`canonical_question`/`answer_ru`/`answer_ky`/
    `category`/`priority`/`handoff_only`/`valid_from`/`valid_until`) are the
    CURRENT, possibly-unpublished, editable values — the live bot only ever serves
    the snapshot of the LATEST `faq_kb_versions` row with
    `action IN ('published','restored')` (see `faq_kb.list_published_candidates`).
    Editing this row does NOT change production until "Publish" is pressed again.

    `enabled`/`publication_status`/`archived_at` are LIVE governance flags (immediate
    effect, no publish cycle needed) — Disable/Enable/Archive act on them directly.
    """

    __tablename__ = "faq_kb_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    canonical_question: Mapped[str] = mapped_column(Text, default="")
    answer_ru: Mapped[str] = mapped_column(Text, default="")
    answer_ky: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(32), default="general", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    publication_status: Mapped[str] = mapped_column(String(16), default="draft", index=True)  # draft|published|archived
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    handoff_only: Mapped[bool] = mapped_column(Boolean, default=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FaqKbVariant(Base):
    """Structured question-variant row (Increment 5) — never a comma-joined string."""

    __tablename__ = "faq_kb_variants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    faq_entry_id: Mapped[int] = mapped_column(ForeignKey("faq_kb_entries.id"), index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)  # ru|ky|None(both)
    normalized_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FaqKbVersion(Base):
    """Snapshot history (Increment 5) — one row per lifecycle event
    (created/edited/published/disabled/enabled/archived/restored). Only rows with
    `action IN ('published','restored')` are ever served to the bot (see
    `app/core/faq_kb.py`); the rest exist purely for audit/history. Publishing and
    rollback never delete or mutate an existing row here."""

    __tablename__ = "faq_kb_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    faq_entry_id: Mapped[int] = mapped_column(ForeignKey("faq_kb_entries.id"), index=True)
    version_number: Mapped[int] = mapped_column(Integer, default=1)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    action: Mapped[str] = mapped_column(String(16), default="created")
    actor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_faq_kb_versions_entry_version", "faq_entry_id", "version_number"),
    )


class FaqKbAnswerLog(Base):
    """Minimal prep for Increment 7 (AI-classifier context) — Increment 5 only logs
    which deterministic FAQ answer was sent, never LLM tokens/cost/model (out of
    scope here, MUST-NOT list). Best-effort write — see `app/core/faq_kb.py`."""

    __tablename__ = "faq_kb_answer_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(Integer, index=True)
    client_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bot_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="faq")
    faq_entry_id: Mapped[int] = mapped_column(Integer, index=True)
    faq_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched_variant_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    match_type: Mapped[str] = mapped_column(String(16), default="")
    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    language: Mapped[str] = mapped_column(String(8), default="ru")
    missing_answer_ky: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiAnswerLog(Base):
    """Increment 6 telegram-pilot: ONE additive table for both §14 LLM usage/cost and
    §15 answer-context — a single structured OpenRouter call produces both at once, so
    there is no separate "classification log" table (that would just duplicate the
    conversation_id/created_at join key for no benefit). Written EXCLUSIVELY by
    `app/core/ai_reply.py` (via `app/core/budget.py::reserve` for the initial placeholder
    row and `app/integrations/panel/ai_log_store.py::finalize` for the final update).

    Never stores the full system prompt, raw model output, or secrets — only the
    structured usage/cost/outcome + the already-validated classification fields (§15).
    `outcome` documents what happened to THIS call: reserved (placeholder, budget
    granted, call not finished yet) | sent | cancelled_by_takeover | validator_blocked |
    budget_exhausted | schema_error | timeout | connection | unauthorized |
    payment_required | http_error | error.
    """

    __tablename__ = "ai_answer_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    generation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("pilot_conversations.id"), nullable=True, index=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True)
    bot_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    model: Mapped[str] = mapped_column(String(128), default="")
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_source: Mapped[str | None] = mapped_column(String(16), nullable=True)   # provider|estimated
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome: Mapped[str] = mapped_column(String(24), default="reserved", index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    client_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bot_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="llm")
    knowledge_entry_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    applied_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lead_temperature: Mapped[str | None] = mapped_column(String(16), nullable=True)
    bot_phase: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dialog_owner: Mapped[str | None] = mapped_column(String(16), nullable=True)
    validator_violations: Mapped[list[str]] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_ai_answer_log_bot_created", "bot_id", "created_at"),
    )


class AnswerContext(Base):
    """Increment 7 telegram-pilot: unified feedback anchor for EVERY automatic answer
    (FAQ match, LLM sent, safe_fallback, budget_fallback, model_error_fallback,
    handoff_only) — see `docs/telegram-pilot-implementation-plan.md` Increment 7 and
    `app/core/feedback_service.py`. Additive — does NOT replace or touch
    `faq_kb_answer_log` (`FaqKbAnswerLog` above) or `ai_answer_log` (`AiAnswerLog`
    above), both of which keep writing exactly as before; this is the ADDITIONAL
    canonical row every automatic Telegram-pilot reply gets, specifically so a
    `feedback_token` can be minted BEFORE the reply is sent and attached to an inline
    keyboard (`app/channels/telegram.py`).

    Written EXCLUSIVELY by `app/integrations/panel/answer_context_store.py`, called
    from the send-wrapping helpers in `app/core/telegram_commands.py` (FAQ/handoff/
    budget_fallback sends) and `app/core/ai_reply.py` (LLM/validator/model-error
    sends) — never directly from a webhook handler.

    NEVER stores: API keys, the full system prompt, the webhook secret, extra PII, or
    the raw BLOCKED model reply (`reply_text` for a `validator_blocked` outcome is the
    SAFE fallback text actually sent, never the raw model output — see
    `app/core/pilot_validator.py::SAFE_FALLBACK_TEXT`)."""

    __tablename__ = "answer_context"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("pilot_conversations.id"), nullable=True, index=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True)
    session_id: Mapped[str] = mapped_column(String(64), default="")
    bot_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    channel: Mapped[str] = mapped_column(String(32), default="telegram")
    # the client's Telegram user_id — needed to re-verify a callback server-side (§7).
    telegram_tester_id: Mapped[str] = mapped_column(String(64), default="")
    chat_id: Mapped[str] = mapped_column(String(64), default="")
    client_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_client_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bot_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_bot_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # faq | llm | safe_fallback | handoff | budget_fallback | model_error_fallback
    source: Mapped[str] = mapped_column(String(24), default="")
    faq_entry_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    faq_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched_variant_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    match_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    knowledge_entry_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reply_text: Mapped[str] = mapped_column(Text, default="")
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    applied_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lead_temperature: Mapped[str | None] = mapped_column(String(16), nullable=True)
    bot_phase: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dialog_owner: Mapped[str | None] = mapped_column(String(16), nullable=True)
    validator_violations: Mapped[list[str]] = mapped_column(JSON, default=list)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    # faq_answered | llm_answered | safe_fallback | validator_blocked | budget_fallback
    # | model_error_fallback | handoff_only
    outcome: Mapped[str] = mapped_column(String(24), default="", index=True)
    feedback_token: Mapped[str] = mapped_column(String(24), default="", unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Feedback(Base):
    """Increment 7 telegram-pilot (+ Increment 7.1 corrective, see
    `migrations/0006_feedback_two_axis.sql`): one tester's rating/comment on ONE
    `AnswerContext` row. `rating` (LEGACY, read-only after the one-time backfill —
    new code never writes it again), `quality_rating`, `strategy_rating` and
    `review_status` are all SEPARATE, INDEPENDENT axes — see
    `app/core/feedback_service.py`. `UNIQUE(answer_context_id, telegram_tester_id)` —
    a re-rating on EITHER axis UPDATEs this same row in place (§8 idempotency), it
    never inserts a second one, and never touches the OTHER axis or the comment."""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    answer_context_id: Mapped[int] = mapped_column(ForeignKey("answer_context.id"), index=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("pilot_conversations.id"), nullable=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True)
    session_id: Mapped[str] = mapped_column(String(64), default="")
    bot_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    telegram_tester_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # LEGACY single-axis value (Increment 7, superseded by the two columns below).
    # correct | inaccurate | incorrect | should_push | should_not_push | should_handoff
    # Kept nullable, never dropped, never written by new code — read-only after the
    # Increment 7.1 backfill (migrations/0006_feedback_two_axis.sql).
    rating: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    # Increment 7.1: independent QUALITY-of-answer axis.
    # correct | inaccurate | incorrect
    quality_rating: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    # Increment 7.1: independent conversation-STRATEGY axis.
    # appropriate | should_push | should_not_push | should_handoff
    strategy_rating: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expected_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expected_handoff: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # unreviewed | in_review | fixed | dismissed
    review_status: Mapped[str] = mapped_column(String(16), default="unreviewed", index=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_feedback_answer_context_tester", "answer_context_id", "telegram_tester_id", unique=True),
    )


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Ленивый singleton фабрики сессий по `settings.database_url`."""
    global _engine, _sessionmaker
    if _sessionmaker is None:
        _engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _sessionmaker


async def init_models(engine: AsyncEngine) -> None:
    """Создать таблицы (дев/тесты без Alembic). В проде схему ведёт Alembic.

    `Base.metadata.create_all` создаёт ВСЕ ORM-модели на этом `Base`, включая новые
    аддитивные `leads`/`pilot_conversations` (Increment 2) и `lead_audit`/`outbox`
    (Increment 3, см. `migrations/0002_lead_status_audit_outbox.sql` — параллельный
    ручной SQL-эквивалент) — отдельного шага для них не требуется.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_columns(conn, "conversations", {
            "phone": "VARCHAR(64) DEFAULT ''",
            "ai_summary": "TEXT DEFAULT ''",
            "manager_next_step": "TEXT DEFAULT ''",
            "escalation_reason": "TEXT DEFAULT ''",
            "lead_temperature": "VARCHAR(16) DEFAULT 'new'",
            "assigned_to": "VARCHAR(64) DEFAULT ''",
            "assigned_at": "TIMESTAMPTZ",
            "outcome": "VARCHAR(24) DEFAULT ''",
            "followup_sent": "BOOLEAN DEFAULT FALSE",
            "archived": "BOOLEAN DEFAULT FALSE",
        })
        await _ensure_columns(conn, "messages", {
            "status": "VARCHAR(16) DEFAULT ''",
            "provider_msg_id": "VARCHAR(128) DEFAULT ''",
            "idempotency_key": "VARCHAR(128) DEFAULT ''",
        })
        await _ensure_columns(conn, "faq_entries", {
            "funnel": "VARCHAR(32)",
            "enabled": "BOOLEAN DEFAULT TRUE",
            "priority": "INTEGER DEFAULT 0",
            "title": "VARCHAR(160) DEFAULT ''",
            "patterns": "JSON DEFAULT '[]'",
            "negative_terms": "JSON DEFAULT '[]'",
            "answer": "TEXT DEFAULT ''",
            "handoff_only": "BOOLEAN DEFAULT FALSE",
            "allow_during_qualification": "BOOLEAN DEFAULT TRUE",
            "updated_by": "VARCHAR(64) DEFAULT ''",
        })
        # Increment 7.1 corrective: `feedback` already existed as of migration 0005 —
        # `create_all` above only creates tables that don't exist yet, so an
        # already-provisioned installation needs these two NEW columns added
        # explicitly (same idempotent upgrade convention as the tables above). A
        # brand-new SQLite/Postgres database gets them for free via `create_all`
        # (the ORM model already carries them) — this call is then a no-op.
        await _ensure_columns(conn, "feedback", {
            "quality_rating": "VARCHAR(24)",
            "strategy_rating": "VARCHAR(24)",
        })
        await backfill_feedback_rating_axes(conn)


async def _ensure_columns(conn, table: str, additions: dict[str, str]) -> None:
    """Идемпотентно добавить недостающие колонки (апгрейд старых инсталляций без Alembic)."""
    existing = await conn.run_sync(
        lambda sync_conn: {c["name"] for c in inspect(sync_conn).get_columns(table)}
    )
    for column, ddl in additions.items():
        if column not in existing:
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


# Increment 7.1 corrective (`migrations/0006_feedback_two_axis.sql`): legacy
# single-axis `feedback.rating` values, split by which NEW axis they belong to.
# `appropriate` has no legacy source -- it stays NULL until a tester taps the new
# "👍 Ведение верное" button.
_FEEDBACK_QUALITY_LEGACY_VALUES = ("correct", "inaccurate", "incorrect")
_FEEDBACK_STRATEGY_LEGACY_VALUES = ("should_push", "should_not_push", "should_handoff")


async def backfill_feedback_rating_axes(conn) -> None:
    """Idempotent backfill mapping legacy `feedback.rating` onto `quality_rating` /
    `strategy_rating` -- mirrors, statement-for-statement, the two UPDATEs in
    `migrations/0006_feedback_two_axis.sql` so the SAME logic is exercised against
    SQLite in tests and Postgres in prod. Only fills a column that is CURRENTLY NULL
    (safe to re-run any number of times, including after a partial application);
    `rating` itself is NEVER modified here."""
    quality_values = ", ".join(f"'{v}'" for v in _FEEDBACK_QUALITY_LEGACY_VALUES)
    strategy_values = ", ".join(f"'{v}'" for v in _FEEDBACK_STRATEGY_LEGACY_VALUES)
    await conn.execute(text(
        f"UPDATE feedback SET quality_rating = rating "
        f"WHERE quality_rating IS NULL AND rating IN ({quality_values})"
    ))
    await conn.execute(text(
        f"UPDATE feedback SET strategy_rating = rating "
        f"WHERE strategy_rating IS NULL AND rating IN ({strategy_values})"
    ))


async def init_db() -> None:
    """Идемпотентно создать схему на боевом движке (вызывается при старте приложения,
    если crm_backend=postgres). Для управляемых миграций в проде — Alembic."""
    get_sessionmaker()  # инициализирует _engine
    assert _engine is not None
    await init_models(_engine)


# STAGING 1 (owner §3): критичные таблицы, наличие которых означает «схема применена».
# Проверяются в readiness — не полный список, а минимальный набор, без которого пилот
# заведомо неработоспособен (диалоги/сообщения + канонические лид/сессия).
_READINESS_CRITICAL_TABLES = ("conversations", "messages", "leads", "pilot_conversations")


async def check_db_ready(sessionmaker: async_sessionmaker | None = None) -> bool:
    """STAGING 1 (owner §3): БД готова = соединение живо (`SELECT 1`) И критичные таблицы
    существуют (схема применена). НИКОГДА не бросает и НИЧЕГО не раскрывает наружу: при
    любой ошибке возвращает False, а деталь (тип исключения, без DSN/паролей) уходит
    только в серверный лог. `sessionmaker` инъектируется в тестах; по умолчанию — боевой."""
    sm = sessionmaker or get_sessionmaker()
    try:
        async with sm() as session:
            await session.execute(text("SELECT 1"))
            names = await session.run_sync(
                lambda sync_session: set(inspect(sync_session.get_bind()).get_table_names())
            )
        return all(t in names for t in _READINESS_CRITICAL_TABLES)
    except Exception:  # noqa: BLE001 — readiness must never raise; detail stays in logs
        import logging
        logging.getLogger("db").warning("readiness check failed", exc_info=True)
        return False

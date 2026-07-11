"""ConversationStore — персистентный лог диалогов для админ-панели.

Источник данных канбана и чат-окна: карточки = диалоги (Conversation), внутри —
человекочитаемый лог сообщений (ConvMessage). Два бэкенда за единым интерфейсом
(паттерн как у StateStore):
- `MemoryConversationStore` — дефолт (тесты, офлайн-демо, один процесс);
- `PostgresConversationStore` — прод (реюз движка из crm/db.py).
Выбор — `settings.panel_backend` (env `PANEL_BACKEND=postgres`); `get_conversation_store()`.

Read-методы возвращают простые dataclass-вью (ConversationView/MessageView), чтобы UI
не зависел от ORM и одинаково работал на обоих бэкендах.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import settings


@dataclass
class MessageView:
    sender: str           # client | bot | manager
    text: str
    created_at: datetime | None = None
    id: int = 0
    status: str = ""              # "" (входящее) | pending|sent|delivered|failed
    provider_msg_id: str = ""


@dataclass
class ConversationView:
    user_id: str              # КЛЮЧ диалога "<bot_id>:<номер>" (или просто номер в дев-демо)
    phone: str = ""           # номер клиента для отображения (fallback — user_id)
    channel: str = ""
    chat_id: str = ""
    bot_id: str = ""
    funnel: str | None = None
    stage: str = "greeting"
    intercepted: bool = False
    archived: bool = False
    qualification: dict[str, Any] = field(default_factory=dict)
    ai_summary: str = ""
    manager_next_step: str = ""
    escalation_reason: str = ""
    lead_temperature: str = "new"
    assigned_to: str = ""         # логин менеджера, ведущего диалог
    outcome: str = ""             # in_progress|office|manager|won|lost
    last_text: str = ""
    last_sender: str = ""
    last_message_at: datetime | None = None
    followup_sent: bool = False       # автодожим уже отправлен (один раз)
    messages: list[MessageView] = field(default_factory=list)


_AUTO_OUTCOMES = {"in_progress", "office", "manager"}
_MANUAL_OUTCOMES = {"won", "lost"}


def _is_auto_downgrade(new_outcome: str, current: str) -> bool:
    """True, если авто-исход пытается перезатереть ручной финал (won/lost) — не даём."""
    return new_outcome in _AUTO_OUTCOMES and current in _MANUAL_OUTCOMES


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryConversationStore:
    """Лог диалогов в памяти процесса (дефолт)."""

    def __init__(self) -> None:
        self._conv: dict[str, ConversationView] = {}
        self._mid = 0
        self._audit: list[dict] = []

    async def ensure(self, user_id: str, channel: str = "", bot_id: str = "",
                     chat_id: str = "", phone: str = "") -> ConversationView:
        conv = self._conv.get(user_id)
        if conv is None:
            conv = ConversationView(user_id=user_id, phone=phone or user_id, channel=channel,
                                    bot_id=bot_id, chat_id=chat_id, last_message_at=_now())
            self._conv[user_id] = conv
        else:
            if chat_id and not conv.chat_id:
                conv.chat_id = chat_id
            if phone and not conv.phone:
                conv.phone = phone
        return conv

    async def add_message(self, user_id: str, sender: str, text: str,
                          channel: str = "", bot_id: str = "", chat_id: str = "",
                          status: str = "", provider_msg_id: str = "",
                          idempotency_key: str = "", phone: str = "") -> int:
        conv = await self.ensure(user_id, channel, bot_id, chat_id, phone)
        if idempotency_key:  # дедуп: повтор той же отправки не создаёт второй записи
            for m in conv.messages:
                if getattr(m, "_idem", "") == idempotency_key:
                    return m.id
        self._mid += 1
        msg = MessageView(sender=sender, text=text, created_at=_now(), id=self._mid,
                          status=status, provider_msg_id=provider_msg_id)
        msg._idem = idempotency_key  # type: ignore[attr-defined]
        conv.messages.append(msg)
        conv.archived = False  # новое сообщение возвращает диалог в рабочие списки
        conv.last_text = text
        conv.last_sender = sender
        conv.last_message_at = _now()
        return msg.id

    async def mark_message_status(self, *, message_id: int | None = None,
                                  provider_msg_id: str | None = None, status: str,
                                  set_provider_msg_id: str | None = None) -> None:
        for conv in self._conv.values():
            for m in conv.messages:
                if (message_id is not None and m.id == message_id) or (
                        provider_msg_id and m.provider_msg_id == provider_msg_id):
                    m.status = status
                    if set_provider_msg_id:
                        m.provider_msg_id = set_provider_msg_id
                    return

    async def update_meta(self, user_id: str, *, funnel: str | None = None,
                          stage: str | None = None, qualification: dict | None = None,
                          intercepted: bool | None = None,
                          ai_summary: str | None = None,
                          manager_next_step: str | None = None,
                          escalation_reason: str | None = None,
                          lead_temperature: str | None = None,
                          assigned_to: str | None = None,
                          outcome: str | None = None,
                          followup_sent: bool | None = None) -> None:
        conv = await self.ensure(user_id)
        if funnel is not None:
            conv.funnel = funnel
        if stage is not None:
            conv.stage = stage
        if qualification is not None:
            conv.qualification = dict(qualification)
        if intercepted is not None:
            conv.intercepted = intercepted
        if ai_summary is not None:
            conv.ai_summary = ai_summary
        if manager_next_step is not None:
            conv.manager_next_step = manager_next_step
        if escalation_reason is not None:
            conv.escalation_reason = escalation_reason
        if lead_temperature is not None:
            conv.lead_temperature = lead_temperature
        if assigned_to is not None:
            conv.assigned_to = assigned_to
        if outcome is not None and not _is_auto_downgrade(outcome, conv.outcome):
            conv.outcome = outcome
        if followup_sent is not None:
            conv.followup_sent = followup_sent

    async def set_intercepted(self, user_id: str, value: bool) -> None:
        await self.update_meta(user_id, intercepted=value)

    async def set_archived(self, user_id: str, value: bool) -> None:
        conv = await self.ensure(user_id)
        conv.archived = value

    async def set_archived_many(self, user_ids: list[str], value: bool = True) -> int:
        count = 0
        for user_id in dict.fromkeys(user_ids):
            conv = self._conv.get(user_id)
            if conv is not None and conv.archived != value:
                conv.archived = value
                count += 1
        return count

    async def all_conversations(self) -> list[ConversationView]:
        return [c for c in self._conv.values() if not c.archived]

    async def list_audit(self, limit: int = 200) -> list[dict]:
        """Последние записи аудита (новые сверху)."""
        return list(reversed(self._audit[-limit:]))

    async def claim(self, user_id: str, manager: str) -> bool:
        """Закрепить диалог за менеджером, если свободен или уже его. True — владеет manager."""
        conv = await self.ensure(user_id)
        if conv.assigned_to in ("", manager):
            conv.assigned_to = manager
            return True
        return False

    async def release_claim(self, user_id: str) -> None:
        conv = await self.ensure(user_id)
        conv.assigned_to = ""

    async def add_audit(self, manager: str, action: str, user_id: str = "", detail: str = "") -> None:
        self._audit.append({"manager": manager, "action": action, "user_id": user_id,
                            "detail": detail, "created_at": _now()})

    async def list_cards(self, funnel: str) -> list[ConversationView]:
        items = [c for c in self._conv.values() if c.funnel == funnel and not c.archived]
        items.sort(key=lambda c: c.last_message_at or _now(), reverse=True)
        return items

    async def get(self, user_id: str) -> ConversationView | None:
        return self._conv.get(user_id)


class PostgresConversationStore:
    """Лог диалогов в Postgres (прод). sessionmaker инъектируется в тестах."""

    def __init__(self, sessionmaker: async_sessionmaker | None = None) -> None:
        self._sessionmaker = sessionmaker

    def _sm(self) -> async_sessionmaker:
        if self._sessionmaker is None:
            from app.integrations.crm.db import get_sessionmaker
            self._sessionmaker = get_sessionmaker()
        return self._sessionmaker

    async def _ensure_row(self, session, user_id: str, channel: str, bot_id: str,
                          chat_id: str = "", phone: str = ""):
        from app.integrations.crm.db import Conversation
        conv = (await session.execute(
            select(Conversation).where(Conversation.user_id == user_id)
        )).scalar_one_or_none()
        if conv is None:
            conv = Conversation(user_id=user_id, phone=phone or user_id, channel=channel,
                                bot_id=bot_id, chat_id=chat_id)
            session.add(conv)
            await session.flush()
        else:
            if chat_id and not conv.chat_id:
                conv.chat_id = chat_id
            if phone and not (conv.phone or ""):
                conv.phone = phone
        return conv

    async def ensure(self, user_id: str, channel: str = "", bot_id: str = "",
                     chat_id: str = "", phone: str = "") -> None:
        async with self._sm()() as session:
            await self._ensure_row(session, user_id, channel, bot_id, chat_id, phone)
            await session.commit()

    async def add_message(self, user_id: str, sender: str, text: str,
                          channel: str = "", bot_id: str = "", chat_id: str = "",
                          status: str = "", provider_msg_id: str = "",
                          idempotency_key: str = "", phone: str = "") -> int:
        from app.integrations.crm.db import ConvMessage
        async with self._sm()() as session:
            conv = await self._ensure_row(session, user_id, channel, bot_id, chat_id, phone)
            if idempotency_key:  # дедуп повторной отправки
                existing = (await session.execute(
                    select(ConvMessage).where(ConvMessage.idempotency_key == idempotency_key)
                )).scalar_one_or_none()
                if existing is not None:
                    return existing.id
            msg = ConvMessage(conversation_id=conv.id, sender=sender, text=text,
                              status=status, provider_msg_id=provider_msg_id,
                              idempotency_key=idempotency_key)
            session.add(msg)
            conv.archived = False  # новое сообщение возвращает диалог в рабочие списки
            conv.last_text = text
            conv.last_sender = sender
            conv.last_message_at = _now()
            await session.commit()
            return msg.id

    async def mark_message_status(self, *, message_id: int | None = None,
                                  provider_msg_id: str | None = None, status: str,
                                  set_provider_msg_id: str | None = None) -> None:
        from app.integrations.crm.db import ConvMessage
        async with self._sm()() as session:
            q = select(ConvMessage)
            if message_id is not None:
                q = q.where(ConvMessage.id == message_id)
            elif provider_msg_id:
                q = q.where(ConvMessage.provider_msg_id == provider_msg_id)
            else:
                return
            msg = (await session.execute(q.limit(1))).scalar_one_or_none()
            if msg is None:
                return
            msg.status = status
            if set_provider_msg_id:
                msg.provider_msg_id = set_provider_msg_id
            await session.commit()

    async def update_meta(self, user_id: str, *, funnel: str | None = None,
                          stage: str | None = None, qualification: dict | None = None,
                          intercepted: bool | None = None,
                          ai_summary: str | None = None,
                          manager_next_step: str | None = None,
                          escalation_reason: str | None = None,
                          lead_temperature: str | None = None,
                          assigned_to: str | None = None,
                          outcome: str | None = None,
                          followup_sent: bool | None = None) -> None:
        async with self._sm()() as session:
            conv = await self._ensure_row(session, user_id, "", "")
            if funnel is not None:
                conv.funnel = funnel
            if stage is not None:
                conv.stage = stage
            if qualification is not None:
                conv.qualification = dict(qualification)
            if intercepted is not None:
                conv.intercepted = intercepted
            if ai_summary is not None:
                conv.ai_summary = ai_summary
            if manager_next_step is not None:
                conv.manager_next_step = manager_next_step
            if escalation_reason is not None:
                conv.escalation_reason = escalation_reason
            if lead_temperature is not None:
                conv.lead_temperature = lead_temperature
            if assigned_to is not None:
                conv.assigned_to = assigned_to
                conv.assigned_at = _now() if assigned_to else None
            if outcome is not None and not _is_auto_downgrade(outcome, conv.outcome or ""):
                conv.outcome = outcome
            if followup_sent is not None:
                conv.followup_sent = followup_sent
            await session.commit()

    async def set_intercepted(self, user_id: str, value: bool) -> None:
        await self.update_meta(user_id, intercepted=value)

    async def set_archived(self, user_id: str, value: bool) -> None:
        async with self._sm()() as session:
            conv = await self._ensure_row(session, user_id, "", "")
            conv.archived = value
            await session.commit()

    async def set_archived_many(self, user_ids: list[str], value: bool = True) -> int:
        ids = list(dict.fromkeys(user_ids))
        if not ids:
            return 0
        from app.integrations.crm.db import Conversation
        async with self._sm()() as session:
            result = await session.execute(
                update(Conversation)
                .where(Conversation.user_id.in_(ids))
                .where(Conversation.archived.is_not(value))
                .values(archived=value)
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def claim(self, user_id: str, manager: str) -> bool:
        """Атомарно закрепить диалог за менеджером, если свободен или уже его."""
        from app.integrations.crm.db import Conversation
        async with self._sm()() as session:
            conv = await self._ensure_row(session, user_id, "", "")
            if (conv.assigned_to or "") in ("", manager):
                conv.assigned_to = manager
                conv.assigned_at = _now()
                await session.commit()
                return True
            return False

    async def release_claim(self, user_id: str) -> None:
        await self.update_meta(user_id, assigned_to="")

    async def all_conversations(self) -> list[ConversationView]:
        """Все диалоги с сообщениями — для аналитики."""
        from app.integrations.crm.db import Conversation
        async with self._sm()() as session:
            rows = (await session.execute(
                select(Conversation).options(selectinload(Conversation.messages))
                .where(Conversation.archived.is_not(True))
            )).scalars().all()
            views = []
            for conv in rows:
                v = _view(conv)
                v.messages = [
                    MessageView(sender=m.sender, text=m.text, created_at=m.created_at,
                                id=m.id, status=getattr(m, "status", "") or "")
                    for m in conv.messages
                ]
                views.append(v)
            return views

    async def add_audit(self, manager: str, action: str, user_id: str = "", detail: str = "") -> None:
        from app.integrations.crm.db import AuditLog
        async with self._sm()() as session:
            session.add(AuditLog(manager=manager, action=action, user_id=user_id, detail=detail))
            await session.commit()

    async def list_audit(self, limit: int = 200) -> list[dict]:
        from app.integrations.crm.db import AuditLog
        async with self._sm()() as session:
            rows = (await session.execute(
                select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
            )).scalars().all()
            return [{"manager": r.manager, "action": r.action, "user_id": r.user_id,
                     "detail": r.detail, "created_at": r.created_at} for r in rows]

    async def list_cards(self, funnel: str) -> list[ConversationView]:
        from app.integrations.crm.db import Conversation
        async with self._sm()() as session:
            rows = (await session.execute(
                select(Conversation).where(Conversation.funnel == funnel)
                .where(Conversation.archived.is_not(True))
                .order_by(Conversation.last_message_at.desc())
            )).scalars().all()
            return [_view(r) for r in rows]

    async def get(self, user_id: str) -> ConversationView | None:
        from app.integrations.crm.db import Conversation
        async with self._sm()() as session:
            conv = (await session.execute(
                select(Conversation).options(selectinload(Conversation.messages))
                .where(Conversation.user_id == user_id)
            )).scalar_one_or_none()
            if conv is None:
                return None
            view = _view(conv)
            view.messages = [
                MessageView(sender=m.sender, text=m.text, created_at=m.created_at,
                            id=m.id, status=getattr(m, "status", "") or "",
                            provider_msg_id=getattr(m, "provider_msg_id", "") or "")
                for m in conv.messages
            ]
            return view


def _view(conv) -> ConversationView:
    """ORM Conversation → ConversationView (без сообщений)."""
    return ConversationView(
        user_id=conv.user_id, phone=(getattr(conv, "phone", "") or conv.user_id),
        channel=conv.channel, chat_id=conv.chat_id, bot_id=conv.bot_id,
        funnel=conv.funnel, stage=conv.stage, intercepted=conv.intercepted,
        archived=getattr(conv, "archived", False) or False,
        qualification=dict(conv.qualification or {}),
        ai_summary=getattr(conv, "ai_summary", "") or "",
        manager_next_step=getattr(conv, "manager_next_step", "") or "",
        escalation_reason=getattr(conv, "escalation_reason", "") or "",
        lead_temperature=getattr(conv, "lead_temperature", "new") or "new",
        assigned_to=getattr(conv, "assigned_to", "") or "",
        outcome=getattr(conv, "outcome", "") or "",
        last_text=conv.last_text,
        last_sender=conv.last_sender, last_message_at=conv.last_message_at,
        followup_sent=getattr(conv, "followup_sent", False) or False,
    )


_memory_store = MemoryConversationStore()
_pg_store: PostgresConversationStore | None = None


def get_conversation_store():
    """Сконфигурированный бэкенд (singleton). По умолчанию — in-memory."""
    global _pg_store
    if settings.panel_backend == "postgres":
        if _pg_store is None:
            _pg_store = PostgresConversationStore()
        return _pg_store
    return _memory_store

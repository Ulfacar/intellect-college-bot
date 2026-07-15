"""ConversationService (Increment 3 телеграм-пилота): `dialog_owner`-переходы
(takeover/release/pause) поверх `leadstore.update_conversation` + `dialog_owner_changed`
аудит.

Инварианты (см. `docs/admin-bot-control-and-ai-classification-spec.md` §3-4/§6):
- `dialog_owner` — источник истины «кто ведёт диалог» (bot/manager/paused). Смена
  owner НЕ меняет `lead_status` и НЕ трогает `bot_phase` автоматически.
- Legacy-совместимость (paused/intercepted) — ТОЛЬКО через существующие read-only
  helpers `leadstore.intercepted_to_dialog_owner`/`dialog_owner_to_intercepted`. Этот
  сервис НИЧЕГО не пишет в legacy `DialogState`/`ConversationView` (`app/core/state.py`,
  `app/integrations/panel/store.py`) — это разведено намеренно (Increment 3 scope).
- release(): `dialog_owner=bot`, `assigned_to` СОХРАНЯЕТСЯ (ответственный менеджер не
  снимается release'ом — только новый takeover сменит `assigned_to`).
- pause(): `dialog_owner=paused`, `assigned_to` НЕ выставляется (в отличие от takeover).
- Повторная установка того же owner — no-op (без audit-записи).

НЕ подключено к `app/admin/router.py`/`app/core/orchestrator.py` в этом инкременте —
только сервис-слой и тесты (см. ограничения Increment 3 в
`docs/telegram-pilot-implementation-plan.md`).
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.integrations.panel.audit_store import get_audit_store
from app.integrations.panel.leadstore import UNSET, PilotConversationView, get_lead_store


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ConversationService:
    def __init__(self, lead_store=None, audit_store=None) -> None:
        self._lead_store = lead_store if lead_store is not None else get_lead_store()
        self._audit_store = audit_store if audit_store is not None else get_audit_store()

    async def _set_owner(
        self, conversation_id: int, new_owner: str, *, assigned_to=UNSET, actor: str | None = None,
        reason: str | None = None, now: datetime | None = None,
    ) -> PilotConversationView | None:
        now = now or _now()
        conv = await self._lead_store.get_conversation(conversation_id)
        if conv is None:
            return None
        previous_owner = conv.dialog_owner

        updated = await self._lead_store.update_conversation(
            conversation_id, dialog_owner=new_owner, assigned_to=assigned_to,
        )

        if previous_owner != new_owner:
            await self._audit_store.record(
                lead_id=conv.lead_id, conversation_id=conversation_id, event_type="dialog_owner_changed",
                previous_owner=previous_owner, new_owner=new_owner, source="admin", actor=actor,
                reason=reason, created_at=now,
            )
        return updated

    async def takeover(
        self, conversation_id: int, manager: str, *, reason: str | None = None, now: datetime | None = None,
    ) -> PilotConversationView | None:
        """`/conversation/{user_id}/takeover`: dialog_owner=manager, assigned_to=manager.
        `lead_status`/`bot_phase` не трогаются."""
        return await self._set_owner(
            conversation_id, "manager", assigned_to=manager, actor=manager, reason=reason, now=now,
        )

    async def release(
        self, conversation_id: int, *, actor: str | None = None, reason: str | None = None,
        now: datetime | None = None,
    ) -> PilotConversationView | None:
        """`/conversation/{user_id}/release`: dialog_owner=bot, assigned_to СОХРАНЯЕТСЯ."""
        return await self._set_owner(
            conversation_id, "bot", assigned_to=UNSET, actor=actor, reason=reason, now=now,
        )

    async def pause(
        self, conversation_id: int, *, actor: str | None = None, reason: str | None = None,
        now: datetime | None = None,
    ) -> PilotConversationView | None:
        """«Поставить на паузу»: dialog_owner=paused, assigned_to НЕ выставляется."""
        return await self._set_owner(
            conversation_id, "paused", assigned_to=UNSET, actor=actor, reason=reason, now=now,
        )

"""Telegram pilot session service (Increment 4).

Wraps `leadstore.get_active_conversation`/`create_new_session` with a per
`(bot_id, external_user_id)` `asyncio.Lock` — mirrors `Orchestrator._lock_for` — so
concurrent webhook deliveries for the same pair (e.g. a fast double-tap, or a retried
delivery that slipped past dedup) cannot race into creating two active sessions.

Active session = pair `(bot_id, external_user_id)` with exactly ONE non-archived
`PilotConversation` (`archived_at is None`), enforced by `leadstore.get_active_conversation`
+ the Increment-3 `ConflictError` guard on `create_conversation`. `chat_id` is recorded on
the Conversation (`external_chat_id`) but `external_user_id` is the primary key for
session lookup — Telegram private chats use `chat_id == user_id`, but we key on user_id to
match the documented invariant (see `docs/telegram-pilot-implementation-plan.md`).

Two entry points, SAME underlying atomic `leadstore.create_new_session` (Increment 4
made it all-or-nothing on both backends — see `app/integrations/panel/leadstore.py`):
- `ensure_active_session` — reuse if one exists, create iff none (§11 "auto first session").
- `start_new_session` — ALWAYS archive current + create fresh (`/newtest`/`/reset`, which
  differ ONLY in reply wording — see `app/core/telegram_commands.py`).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.integrations.panel.leadstore import (
    DEFAULT_LEAD_SOURCE,
    LeadView,
    PilotConversationView,
    get_lead_store,
)

_locks: dict[str, asyncio.Lock] = {}


def _session_key(bot_id: str, external_user_id: str) -> str:
    return f"{bot_id}:{external_user_id}"


def _lock_for(key: str) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = _locks.setdefault(key, asyncio.Lock())
    return lock


@dataclass
class SessionResult:
    conversation: PilotConversationView
    lead: LeadView | None
    created: bool


async def get_active_session(
    bot_id: str, external_user_id: str,
) -> tuple[PilotConversationView | None, LeadView | None]:
    """Read-only lookup — no session created if none exists."""
    store = get_lead_store()
    conv = await store.get_active_conversation(bot_id, external_user_id)
    if conv is None:
        return None, None
    lead = await store.get_lead(conv.lead_id) if conv.lead_id is not None else None
    return conv, lead


async def ensure_active_session(
    bot_id: str, external_user_id: str, *, external_chat_id: str = "", channel: str = "telegram",
    lead_source: str = DEFAULT_LEAD_SOURCE,
) -> SessionResult:
    """Return the active session for `(bot_id, external_user_id)`, creating one
    atomically (Lead+Conversation, linked) iff none exists yet. Repeat calls for the
    same pair reuse the SAME active session — this does NOT create a new session per
    message (§11)."""
    key = _session_key(bot_id, external_user_id)
    async with _lock_for(key):
        store = get_lead_store()
        conv = await store.get_active_conversation(bot_id, external_user_id)
        if conv is not None:
            lead = await store.get_lead(conv.lead_id) if conv.lead_id is not None else None
            return SessionResult(conversation=conv, lead=lead, created=False)
        conv, lead = await store.create_new_session(
            bot_id=bot_id, external_user_id=external_user_id, external_chat_id=external_chat_id,
            channel=channel, lead_source=lead_source,
        )
        return SessionResult(conversation=conv, lead=lead, created=True)


async def start_new_session(
    bot_id: str, external_user_id: str, *, external_chat_id: str = "", channel: str = "telegram",
    lead_source: str = DEFAULT_LEAD_SOURCE,
) -> SessionResult:
    """`/newtest` and `/reset` share this exact call: archive the current active
    session (history is NEVER deleted, the old Lead is NEVER mutated), create a fresh
    Lead{lead_status=new, lead_source=telegram_test, lead_temperature=new} + fresh
    PilotConversation{bot_phase=greeting, dialog_owner=bot}, atomically linked. The
    ONLY difference between the two commands is reply wording — see
    `app/core/telegram_commands.py`."""
    key = _session_key(bot_id, external_user_id)
    async with _lock_for(key):
        store = get_lead_store()
        conv, lead = await store.create_new_session(
            bot_id=bot_id, external_user_id=external_user_id, external_chat_id=external_chat_id,
            channel=channel, lead_source=lead_source,
        )
        return SessionResult(conversation=conv, lead=lead, created=True)


def build_status_snapshot(conv: PilotConversationView, lead: LeadView | None) -> dict:
    """SAFE state only for `/status` — bot_id, session id, lead_status, bot_phase,
    dialog_owner, lead_temperature, qualification (name/grade_base/direction),
    suggested_status, assigned_to, next_action_type/at. NEVER tokens, webhook secrets,
    OpenRouter key, system prompt, stack traces, or other users' dialogs."""
    return {
        "bot_id": conv.bot_id,
        "session_id": conv.session_id,
        "bot_phase": conv.bot_phase,
        "dialog_owner": conv.dialog_owner,
        "assigned_to": conv.assigned_to,
        "lead_status": lead.lead_status if lead else None,
        "lead_temperature": lead.lead_temperature if lead else None,
        "qualification": {
            "name": lead.name if lead else "",
            "grade_base": lead.grade_base if lead else None,
            "direction": lead.direction if lead else None,
        },
        "suggested_status": lead.suggested_status if lead else None,
        "next_action_type": lead.next_action_type if lead else None,
        "next_action_at": lead.next_action_at if lead else None,
    }

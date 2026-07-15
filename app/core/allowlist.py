"""Allowlist тестировщиков Telegram-пилота.

Пилот **закрыт по умолчанию**: если allowlist не настроен (`TELEGRAM_ALLOWED_USER_IDS` и
`TELEGRAM_ALLOWED_CHAT_IDS` пусты), доступ запрещён всем — Telegram-песочница не должна быть
публично доступна. Пользователь вне списка не должен создавать Lead/Conversation и не должен
приводить к вызову LLM (гейт стоит в вебхуке, до `Orchestrator.handle`).
"""
from __future__ import annotations

from app.config import settings


def _as_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def is_allowed(user_id=None, chat_id=None) -> bool:
    """True только если user_id или chat_id входит в настроенный allowlist."""
    users = settings.telegram_allowed_user_ids or []
    chats = settings.telegram_allowed_chat_ids or []
    uid = _as_int(user_id)
    cid = _as_int(chat_id)
    if uid is not None and uid in users:
        return True
    if cid is not None and cid in chats:
        return True
    return False

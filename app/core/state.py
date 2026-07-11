"""Состояние диалога: текущая воронка, этап, собранные данные квалификации.

Два бэкенда за единым интерфейсом `load`/`save`:
- `StateStore` — in-memory словарь (дефолт; тесты, офлайн-демо, один процесс).
- `RedisStateStore` — Redis с TTL (прод: диалоги переживают рестарт/деплой на
  Coolify, переживают несколько воркеров uvicorn).
Выбор бэкенда — через `settings.state_backend` (env `STATE_BACKEND=redis`).
Интерфейс не меняется при замене бэкенда; `get_state_store()` отдаёт нужный.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from app.config import settings


@dataclass
class DialogState:
    user_id: str
    funnel: str | None = None          # tours | visa | tickets
    bot_id: str = ""
    manager_name: str = ""
    stage: str = "greeting"
    qualification: dict[str, Any] = field(default_factory=dict)
    deal_id: str | None = None
    history: list[dict] = field(default_factory=list)  # для контекста LLM
    intercepted: bool = False  # менеджер перехватил диалог в Bitrix → бот молчит
    pending_field: str | None = None  # какой вопрос задан в fallback-режиме (ждём ответ)
    wait_ack_sent: bool = False  # после авто-хендоффа клиенту разово подтвердили ожидание

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "DialogState":
        return cls(**json.loads(raw))


class StateStore:
    """Хранилище состояний диалогов. MVP — память процесса."""

    def __init__(self) -> None:
        self._store: dict[str, DialogState] = {}

    async def load(self, user_id: str) -> DialogState:
        return self._store.setdefault(user_id, DialogState(user_id=user_id))

    async def save(self, state: DialogState) -> None:
        self._store[state.user_id] = state


class RedisStateStore:
    """Персистентное хранилище диалогов в Redis (прод).

    Состояние сериализуется в JSON под ключом `frunze:dialog:<user_id>` и живёт
    `settings.state_ttl_seconds` с момента последнего сообщения (неактивные
    диалоги сами вычищаются). Клиент можно подменить (тесты) — иначе ленивая
    инициализация `redis.asyncio` по `settings.redis_url`.
    """

    KEY_PREFIX = "frunze:dialog:"

    def __init__(self, redis_client: Any = None, ttl: int | None = None) -> None:
        self._redis = redis_client if redis_client is not None else _make_redis()
        self._ttl = ttl if ttl is not None else settings.state_ttl_seconds

    def _key(self, user_id: str) -> str:
        return f"{self.KEY_PREFIX}{user_id}"

    async def load(self, user_id: str) -> DialogState:
        raw = await self._redis.get(self._key(user_id))
        if raw is None:
            return DialogState(user_id=user_id)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return DialogState.from_json(raw)

    async def save(self, state: DialogState) -> None:
        await self._redis.set(self._key(state.user_id), state.to_json(), ex=self._ttl)


def _make_redis() -> Any:
    from redis import asyncio as aioredis  # ленивый импорт: не нужен в memory-режиме

    return aioredis.from_url(settings.redis_url, decode_responses=True)


# In-memory singleton — дефолтный бэкенд (тесты, офлайн-демо).
state_store = StateStore()
_redis_store: RedisStateStore | None = None


def get_state_store() -> StateStore | RedisStateStore:
    """Вернуть сконфигурированный бэкенд (singleton). По умолчанию — in-memory."""
    global _redis_store
    if settings.state_backend == "redis":
        if _redis_store is None:
            _redis_store = RedisStateStore()
        return _redis_store
    return state_store

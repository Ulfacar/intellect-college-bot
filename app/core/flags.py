"""Рантайм-флаги фич (вкл/выкл из админки), переживающие рестарт.

Менеджер переключает фичу кнопкой в панели — значение пишется в БД и читается
джобами на лету (без правки prod.env и передеплоя). Если флаг не выставляли —
берётся дефолт из настроек (env). Два бэкенда за единым интерфейсом, как у
conversation store: memory (дев/тесты) и postgres (прод).
"""
from __future__ import annotations

from app.config import settings


class _MemoryFlags:
    def __init__(self) -> None:
        self._f: dict[str, bool] = {}

    async def get(self, key: str, default: bool) -> bool:
        return self._f.get(key, default)

    async def set(self, key: str, value: bool) -> None:
        self._f[key] = bool(value)

    def _reset(self) -> None:
        self._f.clear()


class _PgFlags:
    def _sm(self):
        from app.integrations.crm.db import get_sessionmaker
        return get_sessionmaker()

    async def get(self, key: str, default: bool) -> bool:
        from sqlalchemy import select
        from app.integrations.crm.db import AppFlag
        async with self._sm()() as session:
            row = (await session.execute(
                select(AppFlag).where(AppFlag.key == key))).scalar_one_or_none()
            return row.value if row is not None else default

    async def set(self, key: str, value: bool) -> None:
        from sqlalchemy import select
        from app.integrations.crm.db import AppFlag
        async with self._sm()() as session:
            row = (await session.execute(
                select(AppFlag).where(AppFlag.key == key))).scalar_one_or_none()
            if row is None:
                session.add(AppFlag(key=key, value=bool(value)))
            else:
                row.value = bool(value)
            await session.commit()


_memory = _MemoryFlags()
_pg: _PgFlags | None = None


def _store():
    global _pg
    if settings.panel_backend == "postgres":
        if _pg is None:
            _pg = _PgFlags()
        return _pg
    return _memory


async def get_flag(key: str, default: bool) -> bool:
    return await _store().get(key, default)


async def set_flag(key: str, value: bool) -> None:
    await _store().set(key, value)


def reset() -> None:
    """Сброс memory-флагов (для тестов)."""
    _memory._reset()

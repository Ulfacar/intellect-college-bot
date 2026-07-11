"""Лёгкий фоновый планировщик на asyncio (без внешних зависимостей).

Запускается из lifespan приложения, раз в TICK вызывает зарегистрированные джобы
(watchdog-алерты, автодожим). Каждая джоба обёрнута в try/except — упавшая не валит
цикл и не мешает другим. Прод — один инстанс, поэтому блокировка не нужна (если
появится горизонтальное масштабирование — добавить Redis-lock здесь).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger("scheduler")

TICK_SECONDS = 300  # как часто прокручивать джобы (5 минут)

Job = Callable[[], Awaitable[None]]
_jobs: list[tuple[str, Job]] = []
_task: asyncio.Task | None = None


def register(name: str, job: Job) -> None:
    """Зарегистрировать периодическую джобу (идемпотентно по имени)."""
    if not any(n == name for n, _ in _jobs):
        _jobs.append((name, job))


async def run_once() -> None:
    """Один прогон всех джоб (используется циклом и тестами)."""
    for name, job in _jobs:
        try:
            await job()
        except Exception:  # noqa: BLE001 — одна джоба не должна валить остальные/цикл
            log.error("scheduler job '%s' failed", name, exc_info=True)


async def _loop() -> None:
    log.info("scheduler started (%d jobs, tick=%ss)", len(_jobs), TICK_SECONDS)
    while True:
        await asyncio.sleep(TICK_SECONDS)
        await run_once()


def start() -> None:
    """Запустить фоновый цикл, если есть джобы и он ещё не запущен."""
    global _task
    if _task is None and _jobs:
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    """Корректно остановить цикл при shutdown."""
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None


def _reset_for_tests() -> None:
    _jobs.clear()

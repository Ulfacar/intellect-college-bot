"""Простой in-memory rate-limiter НЕУДАЧНЫХ попыток логина (защита от брутфорса).

Считаем только провалы: успешный вход не приближает к блокировке. Прод — один
инстанс, поэтому хватает счётчика в памяти процесса (как `_seen_wappi_ids` в
app/main.py). Скользящее окно по IP: не более `limit` провалов за `window` секунд.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

_LOGIN_LIMIT = 10           # провалов
_LOGIN_WINDOW = 60.0        # секунд
_fails: "defaultdict[str, deque[float]]" = defaultdict(deque)


def _prune(ip: str, now: float, window: float) -> "deque[float]":
    q = _fails[ip]
    cutoff = now - window
    while q and q[0] < cutoff:
        q.popleft()
    return q


def is_blocked(ip: str, *, limit: int = _LOGIN_LIMIT,
               window: float = _LOGIN_WINDOW, now: float | None = None) -> bool:
    """Только проверка (без записи): превышен ли лимит провалов за окно."""
    t = now if now is not None else time.monotonic()
    return len(_prune(ip, t, window)) >= limit


def note_failure(ip: str, *, window: float = _LOGIN_WINDOW, now: float | None = None) -> None:
    """Зафиксировать неудачную попытку логина с IP."""
    t = now if now is not None else time.monotonic()
    _prune(ip, t, window).append(t)


def reset() -> None:
    """Очистить счётчики (для тестов)."""
    _fails.clear()

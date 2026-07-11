"""Лёгкие счётчики наблюдаемости (сбои LLM и отправок).

Инкрементируются из оркестратора при сбоях, читаются watchdog'ом (алерты) и
страницей «Статус системы». In-memory процесса (прод — один инстанс), сбрасываются
при рестарте — это ок, нужны для оперативной картины, не для долгой истории.
"""
from __future__ import annotations

import time

_COUNTERS: dict[str, int] = {"llm_failures": 0, "send_failures": 0}
_LAST_TS: dict[str, float] = {"llm_failure_ts": 0.0, "send_failure_ts": 0.0}
_INBOUND: dict[str, float] = {"ts": 0.0}
# Сработки валидатора ответов по виду нарушения (markdown, possible_visa_guarantee, …) —
# чтобы видеть, как часто модель отклоняется от политики, не калеча ответ.
_VALIDATIONS: dict[str, int] = {}


def record_failure(kind: str) -> None:
    """kind: 'llm' | 'send'. Увеличить счётчик и запомнить время последнего сбоя."""
    _COUNTERS[f"{kind}_failures"] = _COUNTERS.get(f"{kind}_failures", 0) + 1
    _LAST_TS[f"{kind}_failure_ts"] = time.time()


def note_validation(kind: str) -> None:
    """Зафиксировать сработку валидатора исходящего ответа (по виду нарушения)."""
    _VALIDATIONS[kind] = _VALIDATIONS.get(kind, 0) + 1


def note_inbound() -> None:
    """Отметить время последнего входящего сообщения клиента (детектор «тишины»)."""
    _INBOUND["ts"] = time.time()


def last_inbound_ago() -> float | None:
    """Сколько секунд назад было последнее входящее (None — ещё не было)."""
    ts = _INBOUND["ts"]
    return round(time.time() - ts, 1) if ts else None


def snapshot() -> dict:
    """Текущее состояние счётчиков (+ «сколько секунд назад был сбой»)."""
    now = time.time()
    out: dict = dict(_COUNTERS)
    for key, ts in _LAST_TS.items():
        out[key] = ts
        out[key.replace("_ts", "_ago")] = round(now - ts, 1) if ts else None
    out["validations"] = dict(_VALIDATIONS)
    return out


def reset() -> None:
    """Сброс (для тестов)."""
    for k in _COUNTERS:
        _COUNTERS[k] = 0
    for k in _LAST_TS:
        _LAST_TS[k] = 0.0
    _INBOUND["ts"] = 0.0
    _VALIDATIONS.clear()

"""Аналитика «ИИ vs менеджер» — чистый расчёт по списку диалогов.

Отделён от хранилища и роутера, чтобы считать одинаково на обоих бэкендах и
покрывать юнит-тестами. На вход — список ConversationView (с messages).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

# Поддерживаемые окна периода для дашборда (ключ → длительность; None = всё время).
PERIODS = [("today", "Сегодня"), ("7d", "7 дней"), ("30d", "30 дней"), ("all", "Всё время")]


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _minutes(a: datetime, b: datetime) -> float:
    return max(0.0, (_aware(b) - _aware(a)).total_seconds() / 60)


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _period_start(period: str, now: datetime) -> datetime | None:
    """Начало окна периода. None — без фильтра (всё время / неизвестный ключ)."""
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "7d":
        return now - timedelta(days=7)
    if period == "30d":
        return now - timedelta(days=30)
    return None


def _activity(convs: list, now: datetime) -> list[dict]:
    """Активность за последние 7 дней (диалогов с сообщением в этот день) — для мини-графика."""
    days = [(now - timedelta(days=i)).date() for i in range(6, -1, -1)]
    counts = {d: 0 for d in days}
    for c in convs:
        dt = _aware(c.last_message_at)
        if dt and dt.date() in counts:
            counts[dt.date()] += 1
    peak = max(counts.values()) if counts else 0
    return [{"label": d.strftime("%d.%m"), "count": counts[d],
             "pct": round(100 * counts[d] / peak) if peak else 0} for d in days]


def compute_analytics(convs: list, period: str = "all", now: datetime | None = None) -> dict:
    """Свод метрик по диалогам: containment, исходы, воронки, время ответа/перехвата.

    period — окно ('today'|'7d'|'30d'|'all'); фильтрует диалоги по last_message_at.
    by_manager — разрез исходов по закреплённому менеджеру (assigned_to).
    """
    now = _aware(now) or datetime.now(timezone.utc)
    start = _period_start(period, now)
    if start is not None:
        convs = [c for c in convs if (_aware(c.last_message_at) or now) >= start]

    total = len(convs)
    # Разрез по менеджерам: сколько диалогов вёл и с каким исходом.
    by_manager: dict[str, Counter] = {}
    contained = 0                      # диалоги без единого сообщения менеджера (вёл только бот)
    outcomes: Counter = Counter()
    by_funnel: dict[str, Counter] = {}
    response_gaps: list[float] = []    # минуты: клиент написал → менеджер ответил
    handoff_gaps: list[float] = []     # минуты: первое сообщение → первое сообщение менеджера
    handoff_reasons: Counter = Counter()

    for c in convs:
        msgs = sorted(c.messages, key=lambda m: (m.created_at or datetime.min.replace(tzinfo=timezone.utc)))
        has_manager = any(m.sender == "manager" for m in msgs)
        if not has_manager:
            contained += 1
        outcomes[c.outcome or "in_progress"] += 1
        by_funnel.setdefault(c.funnel or "—", Counter())[c.stage or "greeting"] += 1
        if getattr(c, "assigned_to", ""):
            mc = by_manager.setdefault(c.assigned_to, Counter())
            mc["handled"] += 1
            mc[c.outcome or "in_progress"] += 1

        # Время ответа менеджера: для каждого manager-сообщения — пауза с предыдущего client.
        last_client_at = None
        first_manager_at = None
        first_at = msgs[0].created_at if msgs else None
        for m in msgs:
            if m.sender == "client":
                last_client_at = m.created_at
            elif m.sender == "manager":
                if first_manager_at is None:
                    first_manager_at = m.created_at
                if last_client_at and m.created_at:
                    response_gaps.append(_minutes(last_client_at, m.created_at))
                last_client_at = None
        if first_at and first_manager_at:
            handoff_gaps.append(_minutes(first_at, first_manager_at))

        if (c.intercepted or c.stage in {"manager", "manager_handoff", "office", "office_consultation"}) \
                and c.escalation_reason:
            handoff_reasons[c.escalation_reason.strip()] += 1

    return {
        "total": total,
        "contained": contained,
        "containment_rate": round(100 * contained / total) if total else 0,
        "outcomes": dict(outcomes),
        "by_funnel": {f: dict(stages) for f, stages in by_funnel.items()},
        "avg_response_min": _avg(response_gaps),
        "avg_handoff_min": _avg(handoff_gaps),
        "handoff_reasons": handoff_reasons.most_common(5),
        "by_manager": {m: dict(c) for m, c in sorted(
            by_manager.items(), key=lambda kv: kv[1]["handled"], reverse=True)},
        "activity": _activity(convs, now),
        "period": period,
    }

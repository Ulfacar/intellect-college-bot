"""Алерт «клиент ждёт живого менеджера».

После хендоффа/перехвата бот в диалоге молчит (решение заказчика). Но если менеджер не
подключается, а клиент продолжает писать — лид тихо теряется. В проде это наблюдали:
серьёзные клиенты (билеты Бишкек→Милан, виза в Германию) писали по 15+ сообщений в пустоту
и не получали ответа. Эта джоба замечает такие диалоги и пингует команду в WhatsApp, чтобы
человек подключился. Анти-дребезг — cooldown по диалогу.

`select_awaiting_targets` — чистая функция отбора (тестируемо). `run` — джоба планировщика.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from app.channels import outbound
from app.config import settings
from app.integrations.panel.store import get_conversation_store

log = logging.getLogger("awaiting")

# Стадии «у человека»: бот здесь молчит, ответить должен менеджер.
_HANDOFF_STAGES = {"manager", "manager_handoff"}
_TERMINAL_OUTCOMES = {"won", "lost"}

# Время последнего алерта по диалогу (in-memory; сбрасывается при рестарте — ок).
_alerted: dict[str, float] = {}


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def select_awaiting_targets(convs: list, now: datetime, cfg) -> list:
    """Диалоги, где клиент ждёт ответа человека дольше порога.

    Условие: диалог передан человеку (стадия manager/handoff ИЛИ менеджер перехватил),
    последним писал КЛИЕНТ (никто не ответил), исход не финальный, и с последнего
    сообщения прошло больше cfg.alert_awaiting_minutes.
    """
    now = _aware(now)
    cutoff = now - timedelta(minutes=cfg.alert_awaiting_minutes)
    out = []
    for c in convs:
        handed_off = (c.stage in _HANDOFF_STAGES) or c.intercepted
        if not handed_off:
            continue
        if c.last_sender != "client":
            continue                       # уже ответил бот/менеджер — клиент не ждёт
        if c.outcome in _TERMINAL_OUTCOMES:
            continue
        last = _aware(c.last_message_at)
        if last is None or last > cutoff:
            continue                       # ещё не намолчался
        out.append(c)
    return out


async def run() -> None:
    """Джоба планировщика: пингнуть команду по брошенным после хендоффа клиентам."""
    from app.core import flags
    if not await flags.get_flag("alerts_enabled", True):
        return  # выключено тумблером в админке
    if not settings.alert_whatsapp_to or not settings.alert_bot_id:
        return  # алерты не настроены (нет номера/бота)

    now_dt = datetime.now(timezone.utc)
    targets = select_awaiting_targets(
        await get_conversation_store().all_conversations(), now_dt, settings)
    now = time.time()
    cooldown = settings.alert_cooldown_minutes * 60
    for c in targets:
        if now - _alerted.get(c.user_id, 0.0) < cooldown:
            continue                       # уже пинговали недавно по этому диалогу
        mins = int((now_dt - _aware(c.last_message_at)).total_seconds() // 60)
        who = c.phone or c.user_id
        text = (f"⏳ Клиент {who} ждёт ответа менеджера ~{mins} мин "
                f"(воронка: {c.funnel or '—'}). Зайдите в панель и ответьте, "
                f"чтобы не потерять лид.")
        try:
            await outbound.send_to_client("whatsapp", settings.alert_bot_id,
                                          settings.alert_whatsapp_to, text)
            _alerted[c.user_id] = now
            log.warning("awaiting alert sent for %s (%d min)", c.user_id, mins)
        except Exception:  # noqa: BLE001 — один сбой не должен останавливать рассылку
            log.error("awaiting alert send failed for %s", c.user_id, exc_info=True)

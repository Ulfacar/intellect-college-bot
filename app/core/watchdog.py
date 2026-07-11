"""Watchdog-алерты: уведомляет админа в WhatsApp при тишине вебхуков или всплеске
сбоев (LLM/отправка). Запускается планировщиком раз в тик.

Решение о том, слать ли алерт, вынесено в чистую `decide()` (тестируемо). `run()`
обвязывает её состоянием и отправкой через outbound. Анти-дребезг — cooldown по типу.
"""
from __future__ import annotations

import logging
import time

from app.channels import outbound
from app.config import settings
from app.core import observ

log = logging.getLogger("watchdog")

# Состояние между тиками: время последнего алерта по типу + база счётчика сбоев.
_state: dict[str, float] = {"alert_silence_ts": 0.0, "alert_fail_ts": 0.0, "fail_baseline": 0.0}


def decide(now: float, last_inbound_ago: float | None, snapshot: dict,
           state: dict, cfg) -> list[tuple[str, str]]:
    """Чистое решение: какие алерты пора слать. Мутирует state (cooldown/база сбоев)."""
    alerts: list[tuple[str, str]] = []
    cooldown = cfg.alert_cooldown_minutes * 60

    # 1) Тишина вебхуков: давно не было входящих.
    silence_limit = cfg.alert_silence_minutes * 60
    if last_inbound_ago is not None and last_inbound_ago >= silence_limit:
        if now - state.get("alert_silence_ts", 0.0) >= cooldown:
            mins = int(last_inbound_ago // 60)
            alerts.append(("silence",
                           f"⚠️ Бот не получал сообщений ~{mins} мин. Проверьте Wappi/вебхуки."))
            state["alert_silence_ts"] = now

    # 2) Всплеск сбоев (LLM + отправка) за период.
    total = snapshot.get("llm_failures", 0) + snapshot.get("send_failures", 0)
    delta = total - state.get("fail_baseline", 0.0)
    if delta >= cfg.alert_fail_threshold and now - state.get("alert_fail_ts", 0.0) >= cooldown:
        alerts.append(("failures",
                       f"⚠️ {int(delta)} сбоев бота за период (LLM/отправка). "
                       f"Проверьте OpenRouter/Wappi."))
        state["alert_fail_ts"] = now
    state["fail_baseline"] = total  # база сдвигается каждый тик → измеряем дельту за тик

    return alerts


async def run() -> None:
    """Джоба планировщика: оценить состояние и при необходимости отправить алерт админу."""
    from app.core import flags
    if not await flags.get_flag("alerts_enabled", True):
        return  # выключено тумблером в админке
    if not settings.alert_whatsapp_to or not settings.alert_bot_id:
        return  # алерты не настроены (нет номера/бота)
    alerts = decide(time.time(), observ.last_inbound_ago(), observ.snapshot(), _state, settings)
    for reason, text in alerts:
        try:
            await outbound.send_to_client("whatsapp", settings.alert_bot_id,
                                          settings.alert_whatsapp_to, text)
            log.error("ALERT[%s]: %s", reason, text)
        except Exception:  # noqa: BLE001
            log.error("watchdog alert send failed", exc_info=True)

"""Автодожим: один мягкий проактивный пинг клиенту, замолчавшему на этапе квалификации.

`select_followup_targets` — чистая функция отбора (тестируемо). `run` — джоба
планировщика: учитывает «тихие часы» (Бишкек), шлёт пинг, двигает карточку в
«Повторное касание» и ставит флаг followup_sent (идемпотентно — пинг ровно один раз).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.channels import outbound
from app.config import settings
from app.core.branding import followup_ping_for
from app.core.leadstate import is_silent
from app.integrations.panel.store import get_conversation_store

log = logging.getLogger("followup")

BISHKEK_UTC_OFFSET = 6  # Кыргызстан UTC+6 (без перехода на летнее время)

def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def is_quiet_hour(local_hour: int, cfg) -> bool:
    """Тихие часы (по локальному времени Бишкека). Интервал может пересекать полночь."""
    a, b = cfg.followup_quiet_from, cfg.followup_quiet_to
    if a == b:
        return False
    if a < b:
        return a <= local_hour < b
    return local_hour >= a or local_hour < b  # окно через полночь (напр. 22→9)


def select_followup_targets(convs: list, now: datetime, cfg) -> list:
    """Кого пора дожать: единая формула «молчит» + ограничения живого WhatsApp."""
    now = _aware(now)
    out = []
    for c in convs:
        if not is_silent(c, now, cfg):
            continue
        if c.channel != "whatsapp" or not (c.chat_id or c.user_id) or not c.bot_id:
            continue                                   # дожимаем только живой WhatsApp-канал
        out.append(c)
    return out


async def run() -> None:
    """Джоба планировщика: разослать дожимы (если включено и не «тихие часы»)."""
    from app.core import flags
    cfg = settings
    if not await flags.get_flag("followup_enabled", cfg.followup_enabled):
        return  # выключено (рантайм-флаг из админки; дефолт — из env)
    now = datetime.now(timezone.utc)
    local_hour = (now + timedelta(hours=BISHKEK_UTC_OFFSET)).hour
    if is_quiet_hour(local_hour, cfg):
        return  # ночь в Бишкеке — переносим на следующий тик

    store = get_conversation_store()
    targets = select_followup_targets(await store.all_conversations(), now, cfg)
    for c in targets:
        text = followup_ping_for(c.funnel)
        try:
            provider = await outbound.send_to_client(
                c.channel, c.bot_id, c.chat_id or c.user_id, text)
            await store.add_message(c.user_id, "bot", text, status="sent",
                                    provider_msg_id=provider or "")
            await store.update_meta(c.user_id, stage="follow_up", followup_sent=True)
            log.info("followup sent to %s (funnel=%s)", c.user_id, c.funnel)
        except Exception:  # noqa: BLE001 — один сбой не должен останавливать рассылку
            log.warning("followup send failed for %s", c.user_id, exc_info=True)

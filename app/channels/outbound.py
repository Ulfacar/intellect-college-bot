"""Исходящая отправка клиенту по каналу диалога — для ответов менеджера из админ-панели.

Резолвит нужный адаптер по `channel` и боту (его профиль/токен) и шлёт сообщение.
Каналы: whatsapp (Wappi), bitrix_openlines (imbot), telegram.
"""
from __future__ import annotations

import logging

from app.channels.bitrix_openlines import BitrixOpenLinesAdapter
from app.channels.telegram import TelegramAdapter
from app.channels.wappi import WappiAdapter
from app.core.bots import registry

logger = logging.getLogger("channels.outbound")


def _adapter_for(channel: str, bot):
    if channel == "whatsapp":
        return WappiAdapter(bot=bot)
    if channel == "bitrix_openlines":
        return BitrixOpenLinesAdapter(bot=bot)
    if channel == "telegram":
        return TelegramAdapter()
    return None


async def send_to_client(channel: str, bot_id: str, chat_id: str, text: str) -> str | None:
    """Отправить текст клиенту в его канал. Возвращает provider_msg_id (Wappi) или None.
    Бросает ValueError при неизвестном канале."""
    bot = registry.by_id(bot_id) if bot_id else None
    adapter = _adapter_for(channel, bot)
    if adapter is None:
        raise ValueError(f"нет адаптера для канала '{channel}'")
    provider_msg_id = await adapter.send(chat_id, text)
    logger.info("manager->client channel=%s chat=%s", channel, chat_id)
    return provider_msg_id

"""Адаптер WhatsApp через Wappi Pro — прямой канал (Схема B, для теста/MVP).

Прод-схема A (WhatsApp → Bitrix Открытые линии → бот) это НЕ отменяет: данный
адаптер — короткий путь к живому WhatsApp-демо на оплаченных номерах Wappi, в
обход Bitrix. Входящее: webhook Wappi (`wh_type=incoming_message`). Исходящее:
`POST /api/sync/message/send?profile_id=<id>` с заголовком Authorization.

⚠ `is_me=true` — это эхо наших же исходящих; такие события игнорируем, иначе бот
ответит сам себе (бесконечный цикл).
"""
from __future__ import annotations

import json
import logging

import httpx

from app.channels.base import Message
from app.config import BotConfig, settings

logger = logging.getLogger("channel.wappi")

_TEXT_TYPE = "chat"  # Wappi: тип текстового сообщения (остальное — медиа/вложения)


def is_delivery_status(raw: dict) -> bool:
    """True для событий статуса доставки/прочтения (не входящее сообщение клиента)."""
    return raw.get("wh_type") in {"messages_status", "message_status", "delivery_status", "ack"}


# Маппинг статусов Wappi → наши (pending|sent|delivered|failed).
_STATUS_MAP = {
    "sent": "sent", "server": "sent", "send": "sent",
    "delivered": "delivered", "device": "delivered", "read": "delivered", "played": "delivered",
    "failed": "failed", "error": "failed", "canceled": "failed",
}


def parse_delivery_status(raw: dict) -> tuple[str, str]:
    """(provider_msg_id, наш_статус) из события статуса Wappi. Пустой статус — если неизвестно."""
    provider_msg_id = str(raw.get("id") or raw.get("message_id") or raw.get("msg_id") or "")
    wappi_status = str(raw.get("status") or raw.get("ack") or "").lower()
    return provider_msg_id, _STATUS_MAP.get(wappi_status, "")


def is_incoming_user_message(raw: dict) -> bool:
    """True только для входящих сообщений клиента в ЛИЧНОМ чате.

    Отсекаем: наши эхо (`is_me`), статусы доставки/авторизации (`wh_type` != incoming_message),
    реакции (`type` == reaction) и групповые чаты (`chat_type` == group) — в группах бот молчит.
    `chat_type` может отсутствовать в синтетических событиях → по умолчанию считаем личным.
    """
    return (
        raw.get("wh_type") == "incoming_message"
        and not raw.get("is_me", False)
        and raw.get("type") != "reaction"
        and raw.get("chat_type", "dialog") != "group"
    )


def _recipient(chat_id: str) -> str:
    """`996700...@c.us` → `996700...` (Wappi recipient = номер); группы (@g.us) как есть."""
    return chat_id.split("@")[0] if chat_id.endswith("@c.us") else chat_id


class WappiAdapter:
    channel = "whatsapp"

    def __init__(self, bot: BotConfig | None = None, client: httpx.AsyncClient | None = None) -> None:
        self.bot = bot
        self._base = settings.wappi_base_url.rstrip("/")
        self._token = settings.wappi_token
        # profile_id — на каждого бота свой (его WhatsApp-номер); legacy-одиночный как fallback.
        self._profile_id = (bot.wappi_profile_id if bot and bot.wappi_profile_id else settings.wappi_profile_id)
        self._client = client

    async def parse(self, raw: dict) -> Message:
        """Разобрать событие Wappi incoming_message в нормализованный Message."""
        chat_id = str(raw.get("chatId") or raw.get("from") or "")
        sender = str(raw.get("from") or "")
        user_id = (sender or chat_id).split("@")[0]
        body = str(raw.get("body") or "")

        if raw.get("type") == _TEXT_TYPE and body:
            kind, text = "text", body
        else:
            kind, text = "non_text", ""  # медиа/голос — бот пока понимает только текст
            # M1-capture: логируем сырой payload медиа, чтобы узнать реальный формат Wappi
            # (ссылка на файл/голос, mime, длительность) — на этом строим плеер в панели.
            try:
                logger.info("Wappi non-text raw [media-capture]: %s",
                            json.dumps(raw, ensure_ascii=False)[:1200])
            except Exception:  # noqa: BLE001 — лог не должен мешать обработке
                logger.info("Wappi non-text raw [media-capture]: type=%s keys=%s",
                            raw.get("type"), sorted(raw.keys()))

        return Message(
            channel=self.channel,
            user_id=user_id,
            chat_id=chat_id,
            text=text,
            kind=kind,
            raw=raw,
        )

    async def send(self, chat_id: str, text: str, **kwargs) -> str:
        """Отправить ответ клиенту через Wappi sync-API. Возвращает provider_msg_id (или "")."""
        if not self._token or not self._profile_id:
            logger.warning("Wappi send пропущен: не заданы token/profile_id")
            return ""

        owns = self._client is None
        client = self._client or httpx.AsyncClient(timeout=20)
        try:
            resp = await client.post(
                f"{self._base}/api/sync/message/send",
                params={"profile_id": self._profile_id},
                headers={"Authorization": self._token, "Content-Type": "application/json"},
                json={"recipient": _recipient(chat_id), "body": text},
            )
            resp.raise_for_status()
            provider_msg_id = _extract_msg_id(resp)
        finally:
            if owns:
                await client.aclose()
        logger.info("Wappi send to=%s profile=%s msg_id=%s",
                    _recipient(chat_id), self._profile_id, provider_msg_id)
        return provider_msg_id


def _extract_msg_id(resp: httpx.Response) -> str:
    """Вытащить id отправленного сообщения из ответа Wappi (ключ варьируется)."""
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 — тело не JSON
        return ""
    if not isinstance(data, dict):
        return ""
    for key in ("message_id", "msg_id", "id"):
        if data.get(key):
            return str(data[key])
    msg = data.get("message")
    if isinstance(msg, dict):
        for key in ("id", "message_id"):
            if msg.get(key):
                return str(msg[key])
    return ""

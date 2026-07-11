"""Адаптер Bitrix24 Открытые линии (прод, ФАЗА 2).

Открытые линии агрегируют WhatsApp / Instagram / Telegram в один поток,
поэтому отдельные адаптеры под эти каналы не нужны — всё приходит сюда.
Один адаптер на бота (своя Открытая линия + свой imbot BOT_ID).

Входящее: событие imbot `ONIMBOTMESSAGEADD` (Bitrix шлёт form-urlencoded с
вложенными ключами `data[PARAMS][DIALOG_ID]=...`). `nest_form()` собирает их в
обычный dict; `parse()` достаёт DIALOG_ID / MESSAGE / FROM_USER_ID.
Исходящее: `imbot.message.add` (DIALOG_ID + BOT_ID бота).

⚠ Точная форма события подтверждается на реальном портале — структура ниже по
документации imbot. `bot_id_from_event`/`nest_form` устойчивы к обоим формам.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.channels.base import Message
from app.config import BotConfig, settings

logger = logging.getLogger("channel.bitrix")


def nest_form(pairs: Any) -> dict:
    """Собрать плоские ключи Bitrix (`a[b][c]=v`) во вложенный dict.

    Принимает list[tuple]/dict (как от form-парсера) или уже готовый вложенный dict
    (тогда возвращает как есть). Числовые сегменты остаются строковыми ключами —
    дальнейший разбор (`bot_id_from_event`) это учитывает.
    """
    if isinstance(pairs, dict) and not any("[" in str(k) for k in pairs):
        return pairs  # уже вложенный (JSON-тест/готовая структура)
    items = pairs.items() if isinstance(pairs, dict) else pairs
    root: dict = {}
    for key, value in items:
        parts = _split_key(str(key))
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return root


def _split_key(key: str) -> list[str]:
    """`data[PARAMS][DIALOG_ID]` → ['data', 'PARAMS', 'DIALOG_ID']."""
    head, _, rest = key.partition("[")
    parts = [head]
    for chunk in rest.split("["):
        parts.append(chunk.rstrip("]"))
    return [p for p in parts if p]


def bot_id_from_event(raw: dict) -> str | None:
    """Достать BOT_ID из события Bitrix imbot для маршрутизации к нужному боту.

    Поддерживает простой/тестовый формат `{"bot_id": ...}` и форму Bitrix
    `data[BOT][<id>][BOT_ID]` (dict или список).
    """
    if raw.get("bot_id") is not None:
        return str(raw["bot_id"])
    data = raw.get("data") or {}
    bot = data.get("BOT")
    if isinstance(bot, dict):
        for value in bot.values():
            if isinstance(value, dict) and value.get("BOT_ID"):
                return str(value["BOT_ID"])
        digit_keys = [k for k in bot if str(k).isdigit()]
        if digit_keys:
            return str(digit_keys[0])
    if isinstance(bot, list) and bot and isinstance(bot[0], dict) and bot[0].get("BOT_ID"):
        return str(bot[0]["BOT_ID"])
    return None


# Признаки не-текстового сообщения в imbot (вложение/файл вместо текста).
_NON_TEXT_HINTS = ("FILES", "ATTACH")


class BitrixOpenLinesAdapter:
    channel = "bitrix_openlines"

    def __init__(self, bot: BotConfig | None = None, client: httpx.AsyncClient | None = None) -> None:
        self.bot = bot
        self._base = settings.bitrix24_webhook_url.rstrip("/")
        self._client = client

    async def parse(self, raw: dict) -> Message:
        """Разобрать событие ONIMBOTMESSAGEADD в нормализованный Message."""
        event = nest_form(raw)
        data = event.get("data") or {}
        params = data.get("PARAMS") or {}

        dialog_id = str(params.get("DIALOG_ID", ""))
        text = str(params.get("MESSAGE", "") or "")
        from_user = str(params.get("FROM_USER_ID", "") or "")

        if text:
            kind = "text"
        elif any(h in params for h in _NON_TEXT_HINTS):
            kind = "non_text"
        else:
            kind = "text"  # пустой/служебный апдейт — оркестратор не отреагирует

        return Message(
            channel=self.channel,
            user_id=from_user,
            chat_id=dialog_id,
            text=text,
            kind=kind,
            raw=raw,
        )

    async def send(self, chat_id: str, text: str, **kwargs) -> None:
        """Отправить ответ клиенту через imbot.message.add от имени бота."""
        if not self._base:
            logger.warning("Bitrix send пропущен: не задан bitrix24_webhook_url")
            return
        payload: dict[str, Any] = {"DIALOG_ID": chat_id, "MESSAGE": text}
        if self.bot and self.bot.bitrix_bot_id:
            payload["BOT_ID"] = self.bot.bitrix_bot_id

        owns = self._client is None
        client = self._client or httpx.AsyncClient(timeout=20)
        try:
            resp = await client.post(f"{self._base}/imbot.message.add.json", json=payload)
            resp.raise_for_status()
        finally:
            if owns:
                await client.aclose()
        logger.info("Bitrix send dialog=%s bot=%s", chat_id, payload.get("BOT_ID"))

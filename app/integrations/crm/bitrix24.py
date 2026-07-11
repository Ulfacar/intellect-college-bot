"""Bitrix24Crm — реальная интеграция (ФАЗА 2).

Реализует CRMPort через Bitrix24 REST (входящий вебхук портала).
Методы Bitrix REST:
  crm.deal.add                 — создать сделку (CATEGORY_ID = воронка, STAGE_ID)
  crm.deal.update              — двигать по канбану (STAGE_ID)
  crm.timeline.comment.add     — заметка к сделке
  imbot.message.add            — сообщение в Открытые линии от бота

Страховка: реальные CATEGORY_ID/STAGE_ID приходят от заказчика (карты в settings).
Пока карта пустая — деградируем мягко (сделка в воронку по умолчанию, апдейт стадии
пропускаем с предупреждением), чтобы не слать в портал несуществующие id.
HTTP-клиент инъектируется (тесты) — иначе создаётся по `bitrix24_webhook_url`.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger("crm.bitrix24")


class Bitrix24Crm:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._base = settings.bitrix24_webhook_url.rstrip("/")
        self._client = client

    async def _call(self, method: str, payload: dict) -> dict:
        """Вызов REST-метода. Возвращает тело ответа (с ключом `result`)."""
        owns = self._client is None
        client = self._client or httpx.AsyncClient(timeout=20)
        try:
            resp = await client.post(f"{self._base}/{method}.json", json=payload)
            resp.raise_for_status()
            return resp.json()
        finally:
            if owns:
                await client.aclose()

    async def create_lead(self, contact: dict[str, Any], funnel: str, data: dict) -> str:
        fields: dict[str, Any] = {
            "TITLE": f"{funnel}: {contact.get('user_id', 'lead')}",
            "COMMENTS": _format_qualification(data),
        }
        category_id = settings.bitrix_category_by_funnel.get(funnel)
        if category_id:
            fields["CATEGORY_ID"] = category_id
        else:
            logger.warning("Bitrix create_lead: нет CATEGORY_ID для воронки %s — воронка по умолчанию", funnel)

        resp = await self._call("crm.deal.add", {"fields": fields})
        deal_id = str(resp.get("result", ""))
        logger.info("Bitrix create_lead deal=%s funnel=%s", deal_id, funnel)
        return deal_id

    async def update_stage(self, deal_id: str, stage: str) -> None:
        stage_id = settings.bitrix_stage_map.get(stage)
        if not stage_id:
            logger.warning("Bitrix update_stage: нет STAGE_ID для стадии '%s' — пропуск", stage)
            return
        await self._call("crm.deal.update", {"id": deal_id, "fields": {"STAGE_ID": stage_id}})
        logger.info("Bitrix update_stage deal=%s -> %s (%s)", deal_id, stage, stage_id)

    async def add_note(self, deal_id: str, text: str) -> None:
        await self._call(
            "crm.timeline.comment.add",
            {"fields": {"ENTITY_ID": deal_id, "ENTITY_TYPE": "deal", "COMMENT": text}},
        )
        logger.info("Bitrix add_note deal=%s", deal_id)

    async def send_message(self, chat_id: str, text: str, bot_id: str | None = None) -> None:
        """Отправить сообщение клиенту в Открытую линию от имени чат-бота."""
        payload: dict[str, Any] = {"DIALOG_ID": chat_id, "MESSAGE": text}
        if bot_id:
            payload["BOT_ID"] = bot_id
        await self._call("imbot.message.add", payload)
        logger.info("Bitrix send_message dialog=%s bot=%s", chat_id, bot_id)


def _format_qualification(data: dict) -> str:
    """Свернуть собранные поля квалификации в текст комментария к сделке."""
    if not data:
        return ""
    return "\n".join(f"{k}: {v}" for k, v in data.items())

"""CrmStub — заглушка CRM для MVP.

Имитирует «канбан» в памяти + логирует действия. Заменяет Bitrix24 до фазы 2,
чтобы воронки работали end-to-end без реального портала.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("crm.stub")

# Простейший локальный «канбан»: deal_id -> запись.
_DEALS: dict[str, dict] = {}
_counter = 0


class CrmStub:
    async def create_lead(self, contact: dict[str, Any], funnel: str, data: dict) -> str:
        global _counter
        _counter += 1
        deal_id = f"stub-{_counter}"
        _DEALS[deal_id] = {"contact": contact, "funnel": funnel, "stage": "new", "data": data, "notes": []}
        logger.info("CRM create_lead %s funnel=%s data=%s", deal_id, funnel, data)
        return deal_id

    async def update_stage(self, deal_id: str, stage: str) -> None:
        if deal_id in _DEALS:
            _DEALS[deal_id]["stage"] = stage
        logger.info("CRM update_stage %s -> %s", deal_id, stage)

    async def add_note(self, deal_id: str, text: str) -> None:
        if deal_id in _DEALS:
            _DEALS[deal_id]["notes"].append(text)
        logger.info("CRM add_note %s: %s", deal_id, text)

    async def send_message(self, chat_id: str, text: str) -> None:
        logger.info("CRM send_message chat=%s: %s", chat_id, text)

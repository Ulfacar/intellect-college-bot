"""PostgresCrm — персистентная реализация CRMPort поверх SQLAlchemy.

Тот же контракт, что у CrmStub, но сделки переживают рестарт и доступны для
аналитики. В проде источник правды по сделке — Bitrix24; этот слой — собственная
запись бота (зеркало действий + основа отчётов админ-панели).
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.integrations.crm.db import Deal, get_sessionmaker

logger = logging.getLogger("crm.postgres")


class PostgresCrm:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession] | None = None) -> None:
        # sessionmaker подменяем в тестах (SQLite); иначе — ленивый по database_url.
        self._sessionmaker = sessionmaker

    def _sm(self) -> async_sessionmaker[AsyncSession]:
        return self._sessionmaker or get_sessionmaker()

    async def create_lead(self, contact: dict[str, Any], funnel: str, data: dict) -> str:
        async with self._sm()() as session:
            deal = Deal(
                user_id=str(contact.get("user_id", "")),
                funnel=funnel,
                stage="new",
                contact=contact,
                data=data,
                notes=[],
            )
            session.add(deal)
            await session.commit()
            logger.info("CRM create_lead %s funnel=%s data=%s", deal.id, funnel, data)
            return str(deal.id)

    async def update_stage(self, deal_id: str, stage: str) -> None:
        async with self._sm()() as session:
            deal = await self._get(session, deal_id)
            if deal is not None:
                deal.stage = stage
                await session.commit()
            logger.info("CRM update_stage %s -> %s", deal_id, stage)

    async def add_note(self, deal_id: str, text: str) -> None:
        async with self._sm()() as session:
            deal = await self._get(session, deal_id)
            if deal is not None:
                # Реассайн (а не in-place append) — иначе SQLAlchemy не заметит мутацию JSON.
                deal.notes = [*deal.notes, text]
                await session.commit()
            logger.info("CRM add_note %s: %s", deal_id, text)

    async def send_message(self, chat_id: str, text: str) -> None:
        # Исходящие сообщения идут через канальный адаптер (Bitrix/Telegram), не через БД.
        logger.info("CRM send_message chat=%s: %s", chat_id, text)

    @staticmethod
    async def _get(session: AsyncSession, deal_id: str) -> Deal | None:
        try:
            pk = int(deal_id)
        except (TypeError, ValueError):
            return None
        return (await session.execute(select(Deal).where(Deal.id == pk))).scalar_one_or_none()

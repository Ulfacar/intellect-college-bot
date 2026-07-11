"""CRMPort — интерфейс CRM. Реализации: CrmStub (MVP), Bitrix24Crm (фаза 2)."""
from __future__ import annotations

from typing import Any, Protocol


class CRMPort(Protocol):
    async def create_lead(self, contact: dict[str, Any], funnel: str, data: dict) -> str:
        """Создать лид/сделку, вернуть deal_id."""
        ...

    async def update_stage(self, deal_id: str, stage: str) -> None:
        """Сдвинуть сделку по канбану."""
        ...

    async def add_note(self, deal_id: str, text: str) -> None:
        """Добавить заметку/лог к сделке."""
        ...

    async def send_message(self, chat_id: str, text: str) -> None:
        """Отправить сообщение через CRM-канал (Открытые линии). Фаза 2."""
        ...

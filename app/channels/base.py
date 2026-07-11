"""Базовый контракт канала и нормализованное сообщение."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Message:
    """Нормализованное входящее сообщение из любого канала."""
    channel: str            # telegram | whatsapp | bitrix_openlines | ...
    user_id: str            # внешний id пользователя в канале
    chat_id: str            # куда отвечать
    text: str
    kind: str = "text"      # text | non_text (голос/фото/медиа — бот пока понимает только текст)
    raw: dict[str, Any] = field(default_factory=dict)


class ChannelAdapter(Protocol):
    """Порт канала: разобрать входящее и отправить ответ."""

    channel: str

    async def parse(self, raw: dict) -> Message: ...

    async def send(self, chat_id: str, text: str, **kwargs) -> None: ...

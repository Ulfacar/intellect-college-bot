"""Telegram-адаптер (MVP-канал) на aiogram."""
from __future__ import annotations

from aiogram import Bot

from app.channels.base import Message
from app.config import settings


class TelegramAdapter:
    channel = "telegram"

    def __init__(self, token: str | None = None) -> None:
        # Ленивое создание: токен валидируется только при первой отправке, а не при
        # импорте — иначе прод (канал Bitrix, без Telegram-токена) не поднимется.
        self._token = token or settings.telegram_bot_token
        self._bot_inst: Bot | None = None

    @property
    def _bot(self) -> Bot:
        if self._bot_inst is None:
            self._bot_inst = Bot(token=self._token)
        return self._bot_inst

    # Типы вложений, которые бот пока не понимает (отвечаем честным fallback).
    _NON_TEXT_KEYS = ("voice", "audio", "photo", "video", "document",
                      "sticker", "video_note", "location", "contact")

    async def parse(self, raw: dict) -> Message:
        """Разобрать update Telegram в нормализованный Message."""
        msg = raw.get("message") or raw.get("edited_message") or {}
        chat = msg.get("chat", {})
        text = msg.get("text", "") or msg.get("caption", "") or ""
        if text:
            kind = "text"
        elif any(k in msg for k in self._NON_TEXT_KEYS):
            kind = "non_text"
        else:
            kind = "text"  # пустой/служебный апдейт — не реагируем
        return Message(
            channel=self.channel,
            user_id=str(msg.get("from", {}).get("id", "")),
            chat_id=str(chat.get("id", "")),
            text=text,
            kind=kind,
            raw=raw,
        )

    async def send(self, chat_id: str, text: str, **kwargs) -> None:
        await self._bot.send_message(chat_id=int(chat_id), text=text, **kwargs)

"""Telegram-адаптер (MVP-канал) на aiogram."""
from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.channels.base import Message
from app.config import settings


def _build_inline_keyboard(markup: dict[str, Any]) -> InlineKeyboardMarkup:
    """Increment 7: converts the plain Bot-API-shaped dict business logic builds
    (`app/core/feedback_service.py::build_feedback_keyboard`) into aiogram's typed
    `InlineKeyboardMarkup` — business logic stays aiogram-agnostic (and trivially
    testable with a fake adapter that just records the dict it was given)."""
    rows = markup.get("inline_keyboard") or []
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn["text"], callback_data=btn["callback_data"]) for btn in row]
        for row in rows
    ])


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

    async def send(self, chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None, **kwargs) -> str | None:
        """Sends the message and returns Telegram's `message_id` (as a string) when
        available — Increment 6 (`app/core/ai_reply.py`) records it as
        `ai_answer_log.bot_message_id`/panel `provider_msg_id` for delivery tracing.
        `Orchestrator._reply` already treats this return value as optional
        (`provider or None`), so this is a minimal, backward-compatible extension of
        the existing `ChannelAdapter.send` contract — fake adapters in tests that
        still return `None` (or nothing) are unaffected.

        Increment 7: `reply_markup`, when given, is the plain dict
        `app/core/feedback_service.py::build_feedback_keyboard` returns — converted to
        an aiogram `InlineKeyboardMarkup` here (see `_build_inline_keyboard`). `None`
        (the default) sends a plain message exactly as before — fully backward
        compatible for every existing caller/fake adapter."""
        markup = _build_inline_keyboard(reply_markup) if reply_markup else None
        sent = await self._bot.send_message(chat_id=int(chat_id), text=text, reply_markup=markup, **kwargs)
        message_id = getattr(sent, "message_id", None)
        return str(message_id) if message_id is not None else None

    async def answer_callback(self, callback_query_id: str, text: str = "", *, show_alert: bool = False) -> None:
        """Increment 7: acks a Telegram `callback_query` (clears the button spinner)
        — see `app/core/feedback_service.py::_safe_ack`, the only caller."""
        await self._bot.answer_callback_query(
            callback_query_id=callback_query_id, text=text or None, show_alert=show_alert,
        )


# Update-type/chat-type helpers (Increment 4 телеграм-пилота) — работают на сыром
# update-dict ДО parse(), чтобы вебхук мог отфильтровать callback_query/edited_message/
# групповые чаты, вообще не создавая Message/Conversation/Lead (§9 ТЗ инкремента).

def update_kind(raw: dict) -> str:
    """Классифицировать сырой Telegram-update: message | edited_message |
    callback_query | other (неизвестный/служебный тип — не обрабатываем)."""
    if "callback_query" in raw:
        return "callback_query"
    if "edited_message" in raw:
        return "edited_message"
    if "message" in raw:
        return "message"
    return "other"


def chat_type(raw: dict) -> str:
    """`chat.type` из `message`/`edited_message` ("" если поле отсутствует — старые
    тестовые фикстуры не всегда его задают; трактуем "" как приватный чат)."""
    msg = raw.get("message") or raw.get("edited_message") or {}
    return str((msg.get("chat") or {}).get("type", ""))

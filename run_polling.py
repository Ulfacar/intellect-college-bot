"""Локальный запуск бота в Telegram через long-polling (для демо, без вебхука/ngrok).

Запуск:
    pip install -r requirements.txt
    # заполни .env: TELEGRAM_BOT_TOKEN и (для AI-диалога) ANTHROPIC_API_KEY
    python run_polling.py

Без ANTHROPIC_API_KEY воронка «Туры» работает в детерминированном режиме (вопрос-ответ).
С ключом — живой AI-диалог через Claude. TourVisor пока в демо-режиме.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Dispatcher
from aiogram.types import Message as TgMessage

from app.channels.base import Message
from app.channels.telegram import TelegramAdapter
from app.core.orchestrator import Orchestrator

logging.basicConfig(level=logging.INFO)

adapter = TelegramAdapter()
orchestrator = Orchestrator(channel=adapter)
dp = Dispatcher()


@dp.message()
async def on_message(tg: TgMessage) -> None:
    msg = Message(
        channel="telegram",
        user_id=str(tg.from_user.id) if tg.from_user else "",
        chat_id=str(tg.chat.id),
        text=tg.text or "",
    )
    if msg.text:
        await orchestrator.handle(msg)


async def main() -> None:
    await dp.start_polling(adapter._bot)


if __name__ == "__main__":
    asyncio.run(main())

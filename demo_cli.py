"""Локальная демо-консоль: поговорить с ботом прямо в терминале.

Без Telegram/вебхуков — нужен только Python. Если задан OPENROUTER_API_KEY,
бот отвечает через LLM (tool-use); без ключа — детерминированный fallback
(квалификация по списку вопросов), чтобы демо работало офлайн.

Запуск:
    python demo_cli.py

Выход — `exit` / `quit` / Ctrl-C.
"""
from __future__ import annotations

import asyncio
import sys

from app.channels.base import Message
from app.config import BotConfig, settings
from app.core.orchestrator import Orchestrator

# Windows-консоль по умолчанию cp1251 — переключаем на UTF-8, иначе кириллица/спецсимволы падают.
for _stream in (sys.stdout, sys.stdin, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

class ConsoleChannel:
    """Канальный адаптер для терминала: печатает ответы бота."""

    channel = "console"

    async def send(self, chat_id: str, text: str, **kwargs) -> None:
        print(f"\nБот: {text}\n")


async def main() -> None:
    bot = BotConfig(id="demo_admission", scenario="admission", title="Intellect College Demo")
    mode = "LLM" if settings.openrouter_api_key else "офлайн-fallback (без ключа)"
    print("=" * 60)
    print(f"  Intellect College — демо-бот ({mode})")
    print("  Сценарий: admission.  Выход: exit / Ctrl-C")
    print("=" * 60)

    orchestrator = Orchestrator(channel=ConsoleChannel(), bot=bot)
    user_id = "console-user"

    while True:
        try:
            text = input("Вы: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nПока!")
            return
        if text.lower() in {"exit", "quit", "выход"}:
            print("Пока!")
            return
        if not text:
            continue
        msg = Message(channel="console", user_id=user_id, chat_id=user_id, text=text)
        await orchestrator.handle(msg)


if __name__ == "__main__":
    asyncio.run(main())

"""Локальная демо-консоль: поговорить с ботом прямо в терминале.

Без Telegram/Bitrix/вебхуков — нужен только Python. Если задан ANTHROPIC_API_KEY,
бот отвечает живым Claude (tool-use); без ключа — детерминированный fallback
(квалификация по списку вопросов), чтобы демо работало офлайн.

Запуск:
    python demo_cli.py            # воронка определяется по ключевым словам
    python demo_cli.py tours      # форсировать воронку «Туры»
    python demo_cli.py visa       # «Визы»
    python demo_cli.py tickets    # «Билеты»

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

SCENARIOS = {"tours", "visa", "tickets"}


class ConsoleChannel:
    """Канальный адаптер для терминала: печатает ответы бота."""

    channel = "console"

    async def send(self, chat_id: str, text: str, **kwargs) -> None:
        print(f"\nБот: {text}\n")


async def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    # Визы/Туры форсируем через BotConfig (сценарий фиксирует воронку);
    # билеты/без аргумента — дев-режим с keyword-детектом.
    bot = None
    if arg in {"tours", "visa"}:
        bot = BotConfig(id=f"demo_{arg}", scenario=arg)  # type: ignore[arg-type]
    forced = arg if arg in SCENARIOS else None

    mode = "живой Claude" if settings.anthropic_api_key else "офлайн-fallback (без ключа)"
    funnel = forced or "по ключевым словам"
    print("=" * 60)
    print(f"  Frunze Travel — демо-бот ({mode})")
    print(f"  Воронка: {funnel}.  Выход: exit / Ctrl-C")
    print("=" * 60)

    orchestrator = Orchestrator(channel=ConsoleChannel(), bot=bot)
    user_id = "console-user"

    # Билеты нельзя форсировать через BotConfig (scenario только tours|visa),
    # поэтому проставим воронку напрямую в состоянии.
    if forced == "tickets":
        from app.core.state import state_store
        state = await state_store.load(user_id)
        state.funnel = "tickets"

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

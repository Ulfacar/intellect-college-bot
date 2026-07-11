"""Не-текст (голос/фото/медиа): бот честно отвечает, а не молчит.

Тестируем поведение оркестратора (без импорта telegram-адаптера, который тянет aiogram).
"""
import asyncio

from app.channels.base import Message
from app.core.orchestrator import NON_TEXT_FALLBACK, Orchestrator


class FakeChannel:
    channel = "telegram"

    def __init__(self):
        self.sent = []

    async def parse(self, raw):  # pragma: no cover
        ...

    async def send(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


def test_voice_message_gets_fallback():
    ch = FakeChannel()
    msg = Message(channel="telegram", user_id="u1", chat_id="1", text="", kind="non_text")
    asyncio.run(Orchestrator(channel=ch).handle(msg))
    assert ch.sent == [("1", NON_TEXT_FALLBACK)]


def test_empty_update_ignored():
    ch = FakeChannel()
    msg = Message(channel="telegram", user_id="", chat_id="", text="", kind="text")
    asyncio.run(Orchestrator(channel=ch).handle(msg))
    assert ch.sent == []  # служебный апдейт — молчим


def test_telegram_parse_detects_voice():
    from app.channels.telegram import TelegramAdapter
    raw = {"message": {"from": {"id": 7}, "chat": {"id": 7}, "voice": {"file_id": "x"}}}
    adapter = TelegramAdapter.__new__(TelegramAdapter)  # без реального Bot/токена
    adapter.channel = "telegram"
    msg = asyncio.run(TelegramAdapter.parse(adapter, raw))
    assert msg.kind == "non_text" and msg.text == ""


def test_telegram_parse_text_is_text():
    from app.channels.telegram import TelegramAdapter
    raw = {"message": {"from": {"id": 7}, "chat": {"id": 7}, "text": "привет"}}
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter.channel = "telegram"
    msg = asyncio.run(TelegramAdapter.parse(adapter, raw))
    assert msg.kind == "text" and msg.text == "привет"

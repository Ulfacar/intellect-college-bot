"""Contract-тесты: парсер Wappi против РЕАЛЬНЫХ payload'ов (захвачены из прода).

Фиксируют фактический формат `{"messages":[...]}` и поведение фильтров навсегда —
именно расхождение с этим форматом однажды заставило бота молчать на все сообщения.
Новый необработанный формат из прода → новый файл в tests/fixtures/wappi/.
"""
import asyncio
import json
from pathlib import Path

from app.channels.wappi import (
    WappiAdapter,
    is_delivery_status,
    is_incoming_user_message,
    parse_delivery_status,
)

FIX = Path(__file__).parent / "fixtures" / "wappi"


def _events(name: str) -> list[dict]:
    payload = json.loads((FIX / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict) and "messages" in payload  # реальный конверт Wappi
    return payload["messages"]


def test_real_incoming_dialog_is_handled():
    ev = _events("incoming_dialog.json")[0]
    assert is_incoming_user_message(ev) is True
    assert is_delivery_status(ev) is False
    msg = asyncio.run(WappiAdapter().parse(ev))
    assert msg.text == "Добрый день"
    assert msg.user_id == "996555000101"
    assert msg.kind == "text"


def test_real_group_reaction_is_ignored():
    ev = _events("group_reaction.json")[0]
    assert is_incoming_user_message(ev) is False     # группа + реакция → молчим


def test_real_own_echo_is_ignored():
    ev = _events("own_echo.json")[0]
    assert is_incoming_user_message(ev) is False     # наше эхо (is_me) → не отвечаем себе


def test_real_delivery_status_parsed():
    ev = _events("delivery_status.json")[0]
    assert is_delivery_status(ev) is True
    assert is_incoming_user_message(ev) is False
    provider_msg_id, status = parse_delivery_status(ev)
    assert provider_msg_id == "AC93B92A1A00E53B3B8DCA319090F90A"
    assert status == "delivered"


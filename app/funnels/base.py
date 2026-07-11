"""Базовый каркас воронки.

Воронка — детерминированный набор этапов. Внутри этапа диалог ведёт LLM-агент,
вызывая инструменты (см. app/agent/tools.py). Конкретные воронки переопределяют
поля REQUIRED_FIELDS и метод handle().
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from app.channels.base import Message
from app.core.state import DialogState


class Funnel(Protocol):
    name: str

    async def handle(self, msg: Message, state: DialogState) -> str | None:
        """Обработать сообщение в контексте состояния, вернуть текст ответа."""
        ...


def collect_qualification(
    state: DialogState,
    text: str,
    required: list[str],
    ask_for: Callable[[str], str],
) -> str | None:
    """Детерминированный сбор полей квалификации (fallback без LLM).

    Сохраняет ответ на ранее заданный вопрос (`state.pending_field`), затем спрашивает
    следующее недостающее поле. Возвращает текст вопроса, либо None — когда всё собрано
    (тогда воронка переходит к действию: поиск/скоринг/передача менеджеру).
    """
    if state.pending_field:
        state.qualification[state.pending_field] = text
        state.pending_field = None

    missing = [f for f in required if f not in state.qualification]
    if missing:
        state.stage = "qualification"
        state.pending_field = missing[0]
        return ask_for(missing[0])
    return None

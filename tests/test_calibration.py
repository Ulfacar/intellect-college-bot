"""Тесты калибровки под стиль Frunze/GetVisa (офлайн-fallback).

Фиксируем ключевые решения заказчика:
- визы ведут в офис и НЕ обещают процент;
- цены не называются (есть дисклеймер про изменение цены);
- билеты доводят до менеджера.
"""
import asyncio
import re

from app.channels.base import Message
from app.core.branding import GETVISA_EMAIL, GETVISA_OFFICE_ADDRESS
from app.funnels.tickets import TicketsFunnel
from app.funnels.visa import VisaFunnel, office_invitation
from app.core.state import DialogState


def _run(funnel, state, texts):
    """Прогнать список реплик клиента через воронку, вернуть список ответов бота."""
    out = []
    for t in texts:
        msg = Message(channel="console", user_id=state.user_id, chat_id=state.user_id, text=t)
        out.append(asyncio.run(funnel.handle(msg, state)))
    return out


def test_visa_invitation_drives_to_office_without_promising_percent():
    text = office_invitation("низкие")
    assert "консультаци" in text.lower()
    assert GETVISA_OFFICE_ADDRESS in text
    assert GETVISA_EMAIL in text
    # Не обещаем визу и не называем процент.
    assert "%" not in text
    assert "гарант" not in text.lower()


def test_visa_fallback_completes_to_office(monkeypatch):
    monkeypatch.setattr("app.agent.llm.settings.openrouter_api_key", "")
    state = DialogState(user_id="vc1", funnel="visa")
    answers = ["виза", "Саодат", "США", "47", "замужем", "врач",
               "Турция", "одна", "базовый", "июнь", "нет"]
    replies = _run(VisaFunnel(), state, answers)
    assert state.deal_id is not None
    assert "консультаци" in replies[-1].lower()
    # Ни в одной реплике fallback не должно быть числового процента (обещания шанса).
    assert not any(re.search(r"\d+\s*%", r or "") for r in replies)


def test_tickets_fallback_hands_to_manager(monkeypatch):
    monkeypatch.setattr("app.agent.llm.settings.openrouter_api_key", "")
    state = DialogState(user_id="tk1", funnel="tickets")
    replies = _run(TicketsFunnel(), state, ["билет", "Бишкек-Москва", "18-21 июня", "2", "прямой"])
    assert state.deal_id is not None
    assert "менеджер" in replies[-1].lower()
    for field in ("route", "dates", "passengers", "direct_pref"):
        assert field in state.qualification

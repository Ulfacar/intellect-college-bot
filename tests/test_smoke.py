"""Смоук-тесты: воронки отрабатывают квалификацию и доходят до CRM-заглушки."""
import asyncio

from app.channels.base import Message
from app.core.state import DialogState
from app.core.router import detect_funnel
from app.funnels.tours import ToursFunnel
from app.funnels.visa import VisaFunnel, score_visa


def test_router_detects_funnels():
    assert detect_funnel("Хочу тур на море") == "tours"
    assert detect_funnel("Нужна виза в Германию") == "visa"
    assert detect_funnel("Купить авиабилет") == "tickets"
    assert detect_funnel("привет") is None


def test_tours_qualification_then_search():
    funnel = ToursFunnel()
    state = DialogState(user_id="u1", funnel="tours")
    msg = Message(channel="telegram", user_id="u1", chat_id="1", text="тур")

    # Пока поля не собраны — бот задаёт вопросы.
    reply = asyncio.run(funnel.handle(msg, state))
    assert reply and "?" in reply

    # Заполняем все поля и проверяем выход на поиск + CRM.
    state.qualification = {
        "departure_city": "Бишкек", "destination": "Турция", "dates": "июль",
        "tourists": "2", "hotel_stars": "5", "meal": "всё включено", "budget": "1000",
    }
    reply = asyncio.run(funnel.handle(msg, state))
    assert state.deal_id is not None
    assert reply


def test_visa_score_bounds():
    assert 5 <= score_visa({}) <= 95
    # Сильный кейс (есть история поездок, без отказов) > слабого (был отказ).
    strong = score_visa({"prior_countries": "Турция, ОАЭ", "prior_refusal": "нет", "occupation": "врач"})
    weak = score_visa({"prior_refusal": "США, 2018"})
    assert strong > weak


def test_visa_flow_reaches_crm():
    funnel = VisaFunnel()
    state = DialogState(user_id="u2", funnel="visa")
    # Полный опросник GetVisa.
    state.qualification = {
        "name": "Саодат", "country": "США", "age": "47", "marital_status": "замужем",
        "occupation": "врач", "prior_countries": "Турция, ОАЭ", "companions": "одна",
        "english_level": "базовый", "dates": "июнь", "prior_refusal": "нет",
    }
    msg = Message(channel="telegram", user_id="u2", chat_id="2", text="виза")
    reply = asyncio.run(funnel.handle(msg, state))
    assert state.deal_id is not None
    # Клиента ведём на консультацию, процент НЕ обещаем.
    assert "консультаци" in reply.lower()
    assert "%" not in reply

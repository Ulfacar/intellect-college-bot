import asyncio

from app.channels.base import Message
from app.core.router import detect_funnel
from app.core.state import DialogState
from app.funnels.admission import AdmissionFunnel
from app.funnels import get_funnel


def test_router_always_admission():
    assert detect_funnel("Хочу тур на море") == "admission"
    assert detect_funnel("Нужна виза") == "admission"
    assert detect_funnel("привет") == "admission"


def test_registry_returns_admission_funnel():
    assert get_funnel("admission").name == "admission"
    assert get_funnel("unknown").name == "admission"


def test_admission_flow_reaches_crm(monkeypatch):
    monkeypatch.setattr("app.agent.llm.settings.openrouter_api_key", "")
    funnel = AdmissionFunnel()
    state = DialogState(user_id="u2", funnel="admission")
    state.qualification = {
        "name": "Айбек",
        "grade_base": "9",
        "direction": "Программная инженерия и ИИ",
    }
    msg = Message(channel="telegram", user_id="u2", chat_id="2", text="хочу поступать")
    reply = asyncio.run(funnel.handle(msg, state))
    assert state.deal_id is not None
    assert state.stage == "manager"
    assert "менеджеру" in reply

import asyncio

from app.channels.base import Message
from app.core.state import DialogState
from app.funnels.admission import AdmissionFunnel
from app.funnels.base import collect_qualification


def test_collect_qualification_stores_answer_and_advances():
    state = DialogState(user_id="u", funnel="admission")
    required = ["a", "b"]
    ask = {"a": "вопрос A?", "b": "вопрос B?"}.get

    assert collect_qualification(state, "первое сообщение", required, ask) == "вопрос A?"
    assert state.pending_field == "a"
    assert collect_qualification(state, "ответ-на-A", required, ask) == "вопрос B?"
    assert state.qualification["a"] == "ответ-на-A"
    assert collect_qualification(state, "ответ-на-B", required, ask) is None
    assert state.qualification["b"] == "ответ-на-B"
    assert state.pending_field is None


def test_admission_fallback_runs_to_completion(monkeypatch):
    monkeypatch.setattr("app.agent.llm.settings.openrouter_api_key", "")
    funnel = AdmissionFunnel()
    state = DialogState(user_id="offline-1", funnel="admission")

    replies = []
    for text in ["хочу поступать", "9", "Айбек", "Программная инженерия и ИИ"]:
        msg = Message(channel="console", user_id="offline-1", chat_id="offline-1", text=text)
        replies.append(asyncio.run(funnel.handle(msg, state)))

    assert state.stage == "manager"
    assert state.qualification["name"] == "Айбек"
    assert state.qualification["grade_base"] == "9"
    assert state.qualification["direction"] == "Программная инженерия и ИИ"
    assert "менеджеру" in replies[-1]


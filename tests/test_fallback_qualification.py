"""Регресс: офлайн-fallback (без LLM) должен ЗАПОМИНАТЬ ответы и доводить
квалификацию до конца, а не зацикливаться на первом вопросе.
"""
import asyncio

from app.channels.base import Message
from app.core.state import DialogState
from app.funnels.base import collect_qualification
from app.funnels.tours import ToursFunnel


def test_collect_qualification_stores_answer_and_advances():
    state = DialogState(user_id="u", funnel="tours")
    required = ["a", "b"]
    ask = {"a": "вопрос A?", "b": "вопрос B?"}.get

    # Первый ход: ничего не сохранили, спросили A.
    q1 = collect_qualification(state, "первое сообщение", required, ask)
    assert q1 == "вопрос A?"
    assert state.pending_field == "a"

    # Второй ход: ответ записался в A, спросили B.
    q2 = collect_qualification(state, "ответ-на-A", required, ask)
    assert state.qualification["a"] == "ответ-на-A"
    assert q2 == "вопрос B?"

    # Третий ход: ответ записался в B, всё собрано → None.
    done = collect_qualification(state, "ответ-на-B", required, ask)
    assert state.qualification["b"] == "ответ-на-B"
    assert done is None
    assert state.pending_field is None


def test_tours_fallback_runs_to_completion(monkeypatch):
    """Воронка «Туры» без ключа проходит все вопросы и НЕ повторяет первый."""
    monkeypatch.setattr("app.agent.llm.settings.openrouter_api_key", "")
    funnel = ToursFunnel()
    state = DialogState(user_id="offline-1", funnel="tours")

    answers = ["Бишкек", "Турция", "август", "2 взрослых", "5*", "всё включено", "1500$"]
    replies = []
    # Первый ход — опенер, дальше скармливаем ответы по одному.
    for text in ["хочу тур", *answers]:
        msg = Message(channel="console", user_id="offline-1", chat_id="offline-1", text=text)
        replies.append(asyncio.run(funnel.handle(msg, state)))

    # Не должны застрять на одном вопросе.
    questions = [r for r in replies if r and r.endswith("?")]
    assert len(set(questions)) == len(questions), f"повтор вопроса: {questions}"
    # Все поля собраны.
    for field in ("departure_city", "destination", "dates", "tourists", "hotel_stars", "meal", "budget"):
        assert field in state.qualification

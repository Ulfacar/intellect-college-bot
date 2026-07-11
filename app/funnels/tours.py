"""Воронка «Туры».

Этапы: greeting → qualification → search (TourVisor) → branch (office | manager).
Сбор полей и ветвление здесь упрощены — продовую логику ведёт LLM-агент с tool-use.
"""
from __future__ import annotations

from app.channels.base import Message
from app.core.state import DialogState
from app.core.branding import PRICE_DISCLAIMER
from app.funnels.base import collect_qualification
from app.integrations.crm import get_crm
import httpx

from app.agent.llm import llm_enabled
from app.integrations.tourvisor.client import TourVisorClient, TourVisorError

# Порядок вопросов выровнен под реальный флоу менеджеров Frunze (см.
# docs/frunze-dialog-style.md): сначала направление и состав, потом даты и бюджет.
REQUIRED_FIELDS = [
    "destination", "tourists", "dates", "budget",
    "departure_city", "hotel_stars", "meal",
]


class ToursFunnel:
    name = "tours"

    def __init__(self) -> None:
        self.tourvisor = TourVisorClient()

    async def handle(self, msg: Message, state: DialogState) -> str | None:
        # Боевой режим: живой AI-диалог через OpenRouter (tool-use).
        if llm_enabled():
            from app.agent.runner import run_tours_turn
            return await run_tours_turn(state, msg.text)

        # Fallback без ключа: детерминированная квалификация (для тестов/демо офлайн).
        question = collect_qualification(state, msg.text, REQUIRED_FIELDS, _ask_for)
        if question is not None:
            return question

        state.stage = "search"
        try:
            tours = await self.tourvisor.search(state.qualification)
        except (TourVisorError, httpx.HTTPError):
            tours = []  # поиск временно недоступен — не валим диалог, ведём к менеджеру
        crm = get_crm()
        state.deal_id = state.deal_id or await crm.create_lead(
            contact={"user_id": state.user_id}, funnel=self.name, data=state.qualification
        )

        if _is_problem_client(state):
            state.stage = "office"
            await crm.update_stage(state.deal_id, "office_consultation")
            return "Подберём для вас лучший вариант лично — приглашаем в офис на консультацию. 🤝"

        state.stage = "manager"
        await crm.update_stage(state.deal_id, "manager_handoff")
        preview = "\n".join(f"• {t}" for t in tours[:3]) or "(нет вариантов)"
        return (
            f"Подобрала для вас варианты:\n{preview}\n\n{PRICE_DISCLAIMER}\n"
            "Давайте забронируем. Удобнее подойти к нам в офис или оформим тут? "
            "Сейчас подключу менеджера. 👍"
        )


def _ask_for(field: str) -> str:
    questions = {
        "destination": "Здравствуйте! 😊 Какое направление рассматриваете? (страна или «помогите выбрать»)",
        "tourists": "Сколько человек едет: взрослые/дети?",
        "dates": "На какие даты ориентируетесь?",
        "budget": "Подскажите, на какой бюджет рассчитываете?",
        "departure_city": "Откуда удобнее вылет: Бишкек или Алматы?",
        "hotel_stars": "Какую звёздность отеля рассматриваете? (3*, 4*, 5*)",
        "meal": "Какое питание предпочитаете? (завтраки, всё включено…)",
    }
    return questions.get(field, "Расскажите подробнее, пожалуйста.")


def _is_problem_client(state: DialogState) -> bool:
    # [?] Критерии «проблемного клиента» не заданы заказчиком — заглушка.
    return False

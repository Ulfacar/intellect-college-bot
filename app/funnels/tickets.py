"""Воронка «Билеты» (бренд Frunze Travel, авиабилеты).

По реальным диалогам это частый флоу (Бишкек→Москва и т.п.): бот собирает заявку
(маршрут/даты/пассажиры/прямой) и передаёт менеджеру на подбор рейса и оплату —
живые тарифы бот не тянет (нет GDS). См. docs/frunze-dialog-style.md.
"""
from __future__ import annotations

from app.channels.base import Message
from app.agent.llm import llm_enabled
from app.core.branding import PRICE_DISCLAIMER
from app.core.state import DialogState
from app.funnels.base import collect_qualification
from app.integrations.crm import get_crm

REQUIRED_FIELDS = ["route", "dates", "passengers", "direct_pref"]


class TicketsFunnel:
    name = "tickets"

    async def handle(self, msg: Message, state: DialogState) -> str | None:
        # Боевой режим: живой AI-диалог через OpenRouter (tool-use).
        if llm_enabled():
            from app.agent.runner import run_tickets_turn
            return await run_tickets_turn(state, msg.text)

        # Fallback без ключа: детерминированная квалификация (для тестов/демо офлайн).
        question = collect_qualification(state, msg.text, REQUIRED_FIELDS, _ask_for)
        if question is not None:
            return question

        crm = get_crm()
        state.deal_id = state.deal_id or await crm.create_lead(
            contact={"user_id": state.user_id}, funnel=self.name, data=state.qualification
        )
        await crm.update_stage(state.deal_id, "manager_handoff")
        state.stage = "manager"
        return (
            "Спасибо! Передаю заявку менеджеру — он подберёт актуальный рейс "
            "(прямой/с пересадкой, багаж, питание) и пришлёт цену. ✈️\n"
            f"{PRICE_DISCLAIMER}"
        )


def _ask_for(field: str) -> str:
    questions = {
        "route": "Здравствуйте! 😊 Откуда и куда летим?",
        "dates": "На какие числа — туда и обратно?",
        "passengers": "Сколько пассажиров?",
        "direct_pref": "Прямой рейс желателен или можно с пересадкой?",
    }
    return questions.get(field, "Расскажите подробнее, пожалуйста.")

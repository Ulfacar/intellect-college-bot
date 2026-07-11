"""Admission funnel for Intellect IT & Business College."""
from __future__ import annotations

from app.agent.llm import llm_enabled
from app.channels.base import Message
from app.core.state import DialogState
from app.funnels.base import collect_qualification
from app.integrations.crm import get_crm

REQUIRED_FIELDS = ["name", "grade_base", "direction"]


class AdmissionFunnel:
    name = "admission"

    async def handle(self, msg: Message, state: DialogState) -> str | None:
        if llm_enabled():
            from app.agent.runner import run_admission_turn

            return await run_admission_turn(state, msg.text)

        question = collect_qualification(state, msg.text, REQUIRED_FIELDS, _ask_for)
        if question is not None:
            return question

        crm = get_crm()
        if not state.deal_id:
            state.deal_id = await crm.create_lead(
                contact={"user_id": state.user_id},
                funnel=self.name,
                data=state.qualification,
            )
        state.stage = "manager"
        await crm.update_stage(state.deal_id, "manager_handoff")
        return (
            "Спасибо! Передала диалог менеджеру приёмной комиссии — он ответит вам "
            "в этом чате в рабочее время."
        )


def _ask_for(field: str) -> str:
    questions = {
        "name": "Как я могу к вам обращаться? 😊",
        "grade_base": "Подскажите, вы поступаете после 9 или после 11 класса?",
        "direction": (
            "Какое направление вам интересно? У нас IT и бизнес-направления, "
            "могу коротко рассказать."
        ),
    }
    return questions.get(field, "Расскажите подробнее, пожалуйста.")

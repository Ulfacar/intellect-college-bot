"""Воронка «Визы» (бренд Frunze Travel, менеджер Медина).

Этапы: приветствие → опросник → мягкая честная подача → приглашение в офис/онлайн → CRM.
Главная цель — довести клиента до консультации эксперта. БЕЗ обещаний по визе; цены не называем.
Откалибровано по реальным диалогам (см. docs/frunze-dialog-style.md).
"""
from __future__ import annotations

from app.channels.base import Message
from app.agent.llm import llm_enabled
from app.core.branding import GETVISA_EMAIL, GETVISA_OFFICE_ADDRESS
from app.core.state import DialogState
from app.funnels.base import collect_qualification
from app.integrations.crm import get_crm

# Опросник Frunze Travel из реальных диалогов Медины.
REQUIRED_FIELDS = [
    "name", "country", "age", "marital_status", "occupation",
    "prior_countries", "companions", "english_level", "dates", "prior_refusal",
]


class VisaFunnel:
    name = "visa"

    async def handle(self, msg: Message, state: DialogState) -> str | None:
        # Боевой режим: живой AI-диалог через OpenRouter (tool-use).
        if llm_enabled():
            from app.agent.runner import run_visa_turn
            return await run_visa_turn(state, msg.text)

        # Fallback без ключа: детерминированный опросник (для тестов/демо офлайн).
        question = collect_qualification(state, msg.text, REQUIRED_FIELDS, _ask_for)
        if question is not None:
            return question

        state.stage = "scoring"
        crm = get_crm()
        state.deal_id = state.deal_id or await crm.create_lead(
            contact={"user_id": state.user_id}, funnel=self.name, data=state.qualification
        )
        await crm.update_stage(state.deal_id, "office_consultation")
        # Внутренний сигнал силы кейса влияет только на тон — клиенту % не обещаем.
        return office_invitation(visa_category(score_visa(state.qualification)))


def office_invitation(category: str) -> str:
    """Мягкое честное приглашение на консультацию (без обещаний и без цен)."""
    if category == "высокие":
        opener = "Спасибо! По вашим данным кейс выглядит перспективно 😊"
    elif category == "средние":
        opener = "Спасибо за ответы! С вашим кейсом точно есть с чем работать."
    else:
        opener = "Спасибо за ответы! Случай требует внимательной подготовки — но это решаемо."
    return (
        f"{opener} Многое зависит от грамотно заполненной анкеты и подготовки к интервью — "
        "именно это мы и берём на себя. Давайте пригласим вас на консультацию к эксперту: "
        f"можно подойти к нам в офис ({GETVISA_OFFICE_ADDRESS}) или начать онлайн. "
        f"Документы можно прислать на {GETVISA_EMAIL}. Когда вам удобно? 🙏"
    )


def score_visa(data: dict) -> int:
    """ВНУТРЕННИЙ ориентир силы кейса (0–100) — только для выбора тона бота.

    Клиенту точный процент НЕ показываем и визу не обещаем (см. office_invitation).
    [?] Методика заказчиком не задана — это эвристика, не обещание.
    """
    base = 50
    if data.get("prior_countries"):
        base += 15  # есть история поездок — плюс
    if str(data.get("prior_refusal", "")).strip().lower() in {"", "нет", "не было", "no"}:
        base += 15  # не было отказов — плюс
    else:
        base -= 15  # был отказ — минус
    if data.get("occupation"):
        base += 5
    return max(5, min(95, base))


def visa_category(pct: int) -> str:
    """% → внутренняя категория (для тона; клиенту число не озвучиваем)."""
    if pct >= 70:
        return "высокие"
    if pct >= 45:
        return "средние"
    return "низкие"


def _ask_for(field: str) -> str:
    questions = {
        "name": "Здравствуйте! Меня зовут Медина, я ваш личный визовый эксперт Frunze Travel 😊 Как могу к вам обращаться?",
        "country": "Виза в какую страну вас интересует?",
        "age": "Подскажите, сколько вам лет?",
        "marital_status": "Ваше семейное положение? (в браке / не в браке, есть ли дети)",
        "occupation": "Кем работаете или где учитесь?",
        "prior_countries": "Какие страны посещали ранее? (если были визы — какие)",
        "companions": "Поедете один(одна) или с семьёй?",
        "english_level": "Как у вас с английским — свободно, базово, не владеете?",
        "dates": "На какие даты планируете поездку?",
        "prior_refusal": "Были ли ранее отказы в визе? Если да — в какую страну и в каком году?",
    }
    return questions.get(field, "Расскажите, пожалуйста, подробнее.")

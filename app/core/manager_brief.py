"""Manager-facing brief for the admin chat card.

The bot's primary job is to qualify and warm up a lead before a manager or the
office takes over. This module turns current dialog state into a compact,
operator-readable summary without relying on the LLM response format.
"""
from __future__ import annotations

from app.core.state import DialogState


FIELD_LABELS = {
    "name": "имя",
    "country": "страна",
    "age": "возраст",
    "marital_status": "семейное положение",
    "occupation": "работа/учеба",
    "prior_countries": "визовая/туристическая история",
    "companions": "кто едет",
    "english_level": "английский",
    "dates": "даты",
    "prior_refusal": "отказы",
    "destination": "направление",
    "tourists": "туристы",
    "budget": "бюджет",
    "departure_city": "город вылета",
    "hotel_stars": "звезды отеля",
    "meal": "питание",
    "visit_time": "время визита",
    "office_visit": "визит в офис",
    "selected_option": "выбранный вариант",
    "route": "маршрут",
    "passengers": "пассажиры",
    "baggage": "багаж",
}


def build_manager_brief(state: DialogState) -> dict[str, str]:
    """Return compact fields shown in the admin panel."""
    hot_signal = _latest_user_text_has_hot_signal(state)
    if not state.funnel:
        return {
            "ai_summary": "Клиент написал, направление еще не определено.",
            "manager_next_step": "Дождаться ответа клиента или уточнить: тур, виза или билеты.",
            "escalation_reason": "Клиент уже проявил готовность к действию." if hot_signal else "",
            "lead_temperature": "hot" if hot_signal else "new",
        }

    known = _known_fields(state.qualification)
    summary = _summary(state.funnel, known)
    next_step = _next_step(state, hot_signal)
    reason = _escalation_reason(state, hot_signal)
    temperature = _temperature(state, hot_signal)
    return {
        "ai_summary": summary,
        "manager_next_step": next_step,
        "escalation_reason": reason,
        "lead_temperature": temperature,
    }


def _known_fields(data: dict) -> list[str]:
    parts: list[str] = []
    for key, value in data.items():
        if value in (None, "", [], {}):
            continue
        label = FIELD_LABELS.get(key, key)
        parts.append(f"{label}: {value}")
    return parts


def _summary(funnel: str, known: list[str]) -> str:
    prefix = {
        "visa": "Визовый лид",
        "tours": "Лид на тур",
        "tickets": "Лид на билеты",
    }.get(funnel, "Лид")
    if not known:
        return f"{prefix}: бот начал квалификацию, данных пока мало."
    return f"{prefix}. Уже собрано: " + "; ".join(known[:8]) + "."


def _next_step(state: DialogState, hot_signal: bool = False) -> str:
    if hot_signal:
        return "Горячий клиент: подключиться вручную и зафиксировать офис/созвон/бронь."
    if state.intercepted:
        return "Диалог у менеджера: ответить клиенту вручную и довести до офиса/оплаты."
    if state.stage in {"follow_up", "followup", "callback"}:
        return "Сделать короткое повторное касание и вернуть клиента к следующему шагу."
    if state.stage in {"office", "office_consultation"}:
        return "Согласовать удобное время консультации в офисе или онлайн."
    if state.stage in {"manager", "manager_handoff"}:
        return "Подключиться и дожать: бронь, оплата, запись или точный следующий шаг."
    if state.stage in {"search", "scoring", "visa_scoring"}:
        return "Проверить собранные данные и подготовить предложение/консультацию."
    return "Продолжить квалификацию: бот собирает недостающие ответы."


def _escalation_reason(state: DialogState, hot_signal: bool = False) -> str:
    data = {k: str(v).lower() for k, v in state.qualification.items() if v is not None}
    if hot_signal:
        return "Клиент сам показал готовность к оплате, брони, офису или созвону."
    if state.stage in {"follow_up", "followup", "callback"}:
        return "Нужно вернуть клиента после паузы: такие лиды часто дозревают через повторное касание."
    if state.stage in {"office", "office_consultation"}:
        return "Бот ведет клиента к консультации в офисе/онлайн."
    if state.stage in {"manager", "manager_handoff"}:
        return "Нужен живой менеджер для дожима или ручного подбора."
    if "prior_refusal" in data and data["prior_refusal"] not in {"нет", "не было", "no", ""}:
        return "В визовом кейсе указан отказ - нужен внимательный разбор."
    budget = data.get("budget", "")
    if any(marker in budget for marker in ("деш", "миним", "недорог", "самый")):
        return "Клиент чувствителен к бюджету - важно аккуратно сверить ожидания."
    return ""


def _temperature(state: DialogState, hot_signal: bool = False) -> str:
    if hot_signal:
        return "hot"
    if state.intercepted or state.stage in {"manager", "manager_handoff"}:
        return "hot"
    if state.stage in {"office", "office_consultation"}:
        return "warm"
    if state.stage in {"follow_up", "followup", "callback"}:
        return "warm"
    if len([v for v in state.qualification.values() if v]) >= 4:
        return "warm"
    return "new"


def _latest_user_text_has_hot_signal(state: DialogState) -> bool:
    text = _latest_user_text(state).lower()
    if not text:
        return False
    markers = (
        "готов оплат", "оплат", "деньги", "перевед", "перевод",
        "брон", "заброн", "покуп", "берем", "берём",
        "подойти в офис", "приду", "можно подойти", "запишите", "запис",
        "созвон", "позвон", "стоимость не имеет значения", "главное визу",
    )
    return any(marker in text for marker in markers)


def _latest_user_text(state: DialogState) -> str:
    for item in reversed(state.history):
        if item.get("role") == "user" and isinstance(item.get("content"), str):
            return item["content"]
    return ""

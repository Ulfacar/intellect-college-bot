"""Manager-facing brief for the admissions admin card."""
from __future__ import annotations

from app.core.state import DialogState

FIELD_LABELS = {
    "name": "имя",
    "grade_base": "база",
    "direction": "направление",
    "visit_time": "удобное время",
    "escalation_reason": "причина эскалации",
}


def build_manager_brief(state: DialogState) -> dict[str, str]:
    hot_signal = _latest_user_text_has_hot_signal(state)
    if not state.funnel:
        return {
            "ai_summary": "Клиент написал, сценарий приёмной ещё не зафиксирован.",
            "manager_next_step": "Уточнить базу поступления: после 9 или 11 класса.",
            "escalation_reason": "Клиент готов к следующему шагу." if hot_signal else "",
            "lead_temperature": "hot" if hot_signal else "new",
        }

    known = _known_fields(state.qualification)
    return {
        "ai_summary": _summary(known),
        "manager_next_step": _next_step(state, hot_signal),
        "escalation_reason": _escalation_reason(state, hot_signal),
        "lead_temperature": _temperature(state, hot_signal),
    }


def _known_fields(data: dict) -> list[str]:
    parts: list[str] = []
    for key, value in data.items():
        if value in (None, "", [], {}):
            continue
        label = FIELD_LABELS.get(key, key)
        parts.append(f"{label}: {value}")
    return parts


def _summary(known: list[str]) -> str:
    if not known:
        return "Абитуриент: бот начал квалификацию, данных пока мало."
    return "Абитуриент. Уже собрано: " + "; ".join(known[:8]) + "."


def _next_step(state: DialogState, hot_signal: bool = False) -> str:
    if hot_signal:
        return "Горячий клиент: подключиться и подтвердить следующий шаг по тесту."
    if state.intercepted:
        return "Диалог у менеджера: ответить клиенту вручную."
    if state.stage in {"follow_up", "followup", "callback"}:
        return "Сделать короткое повторное касание и вернуть к поступлению."
    if state.stage in {"test_invite", "office", "office_consultation"}:
        return "Подтвердить дату, время и формат вступительного теста."
    if state.stage in {"manager", "manager_handoff"}:
        return "Ответить на вопрос, который бот передал менеджеру."
    return "Продолжить квалификацию: база, имя, направление."


def _escalation_reason(state: DialogState, hot_signal: bool = False) -> str:
    explicit = state.qualification.get("escalation_reason")
    if explicit:
        return str(explicit)
    if hot_signal:
        return "Клиент сам показал готовность к тесту или оформлению."
    if state.stage in {"test_invite", "office", "office_consultation"}:
        return "Клиент приглашён на вступительный тест."
    if state.stage in {"manager", "manager_handoff"}:
        return "Нужен живой менеджер приёмной комиссии."
    return ""


def _temperature(state: DialogState, hot_signal: bool = False) -> str:
    if hot_signal:
        return "hot"
    if state.intercepted or state.stage in {"manager", "manager_handoff", "test_invite"}:
        return "hot"
    if state.stage in {"follow_up", "followup", "callback"}:
        return "warm"
    if len([v for v in state.qualification.values() if v]) >= 2:
        return "warm"
    return "new"


def _latest_user_text_has_hot_signal(state: DialogState) -> bool:
    text = _latest_user_text(state).lower()
    markers = (
        "запишите", "запис", "тест", "хочу поступ", "поступать", "готов",
        "приду", "подойти", "оформ", "оплат", "тестке", "тапшыр",
    )
    return any(marker in text for marker in markers)


def _latest_user_text(state: DialogState) -> str:
    for item in reversed(state.history):
        if item.get("role") == "user" and isinstance(item.get("content"), str):
            return item["content"]
    return ""

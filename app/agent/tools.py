"""Tool schemas for the admission LLM agent."""
from __future__ import annotations


def tools_for(names: list[str]) -> list[dict]:
    return [t for t in TOOLS if t["name"] in names]


TOOLS = [
    {
        "name": "ask_qualification",
        "description": (
            "Зафиксировать собранные данные абитуриента и задать следующий вопрос "
            "квалификации. Вызывай, как только клиент сообщил имя, базу (9/11 класс) "
            "или направление — даже вперемешку с другим вопросом."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "enum": ["name", "grade_base", "direction"],
                    "description": "Какое поле собираешься спросить следующим.",
                },
                "question": {"type": "string", "description": "Формулировка вопроса клиенту."},
                "name": {"type": "string", "description": "Имя, если клиент уже назвал."},
                "grade_base": {
                    "type": "string",
                    "enum": ["9", "11"],
                    "description": "База поступления, если уже известна.",
                },
                "direction": {
                    "type": "string",
                    "description": "Интересующее направление или «не определился».",
                },
            },
            "required": ["field", "question"],
        },
    },
    {
        "name": "escalate_to_office",
        "description": (
            "Пригласить абитуриента на вступительный тест / в колледж: клиент готов "
            "записаться или прийти. Фиксирует намерение; дату и формат подтверждает менеджер."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "name": {"type": "string", "description": "Имя клиента, если названо."},
                "grade_base": {"type": "string", "enum": ["9", "11"]},
                "direction": {"type": "string"},
                "visit_time": {"type": "string", "description": "Когда клиенту удобно, если сам сказал."},
            },
            "required": ["reason"],
        },
    },
    {
        "name": "handoff_to_manager",
        "description": (
            "Передать диалог живому менеджеру приёмной: вопрос вне базы знаний, торг о цене/"
            "скидке, оплата/договор, жалоба, просьба человека, неуверенность, 2 хода без "
            "осмысленного ответа."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Краткая причина для карточки."}
            },
        },
    },
    {
        "name": "crm_update_stage",
        "description": "Сдвинуть карточку абитуриента по канбану (только стадии бота).",
        "input_schema": {
            "type": "object",
            "properties": {
                "stage": {"type": "string", "enum": ["qualification", "consulting", "test_invite"]}
            },
            "required": ["stage"],
        },
    },
]

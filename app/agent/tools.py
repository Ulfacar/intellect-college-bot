"""Описания инструментов (tool-use) для LLM-агента + диспетчер вызовов.

Контракты соответствуют PRD §4. Реализации делегируют в воронки/интеграции.
"""
from __future__ import annotations


def tools_for(names: list[str]) -> list[dict]:
    """Подмножество инструментов по именам (для конкретной воронки)."""
    return [t for t in TOOLS if t["name"] in names]


TOOLS = [
    {
        "name": "ask_qualification",
        "description": "Задать клиенту следующий уточняющий вопрос для квалификации.",
        "input_schema": {
            "type": "object",
            "properties": {"field": {"type": "string"}, "question": {"type": "string"}},
            "required": ["field", "question"],
        },
    },
    {
        "name": "search_tours",
        "description": "Найти актуальные туры в TourVisor по собранным параметрам.",
        "input_schema": {
            "type": "object",
            "properties": {
                "destination": {"type": "string"},
                "region": {
                    "type": "string",
                    "description": ("Курорт/регион внутри страны (напр. «Анталья», «Кемер», "
                                    "«Аланья», «Сиде»). Для пляжного отдыха указывай конкретный "
                                    "курорт, иначе поиск выдаст города (Стамбул), а не море."),
                },
                "meal": {
                    "type": "string",
                    "description": ("Тип питания: «всё включено», «ультра всё включено», "
                                    "«завтраки», «полупансион», «полный пансион»."),
                },
                "dates": {"type": "string"},
                "tourists": {"type": "string"},
                "children_ages": {
                    "type": "string",
                    "description": ("Возраст КАЖДОГО ребёнка через запятую, напр. «10, 8, 5». "
                                    "Обязательно, если едут дети — без возрастов поиск вернёт пусто."),
                },
                "budget": {"type": "string"},
                "departure_city": {"type": "string"},
            },
            "required": ["destination"],
        },
    },
    {
        "name": "score_visa",
        "description": ("Внутренняя оценка силы визового кейса по собранным данным (для тона "
                        "ответа). Клиенту точный процент не показывается."),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "country": {"type": "string"},
                "age": {"type": "string"},
                "marital_status": {"type": "string"},
                "occupation": {"type": "string"},
                "prior_countries": {"type": "string"},
                "companions": {"type": "string"},
                "english_level": {"type": "string"},
                "dates": {"type": "string"},
                "prior_refusal": {"type": "string"},
            },
            "required": ["country"],
        },
    },
    {
        "name": "escalate_to_office",
        "description": "Пригласить клиента в офис (сложный/проблемный случай).",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "name": {
                    "type": "string",
                    "description": "Имя клиента, если уже спросили и клиент его назвал.",
                },
                "visit_time": {
                    "type": "string",
                    "description": "Когда клиент планирует прийти/созвониться, например «завтра в 15:00».",
                },
                "selected_option": {
                    "type": "string",
                    "description": "Выбранный тур/отель или кратко что клиент хочет обсудить в офисе.",
                },
            },
            "required": ["reason"],
        },
    },
    {
        "name": "handoff_to_manager",
        "description": "Передать тёплого клиента живому менеджеру на дожим.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "submit_request",
        "description": "Зафиксировать заявку клиента на авиабилеты и передать менеджеру на подбор.",
        "input_schema": {
            "type": "object",
            "properties": {
                "route": {"type": "string"},
                "dates": {"type": "string"},
                "passengers": {"type": "string"},
                "direct_pref": {"type": "string"},
            },
            "required": ["route"],
        },
    },
    {
        "name": "crm_update_stage",
        "description": "Сдвинуть сделку по канбану CRM.",
        "input_schema": {
            "type": "object",
            "properties": {"stage": {"type": "string"}},
            "required": ["stage"],
        },
    },
]

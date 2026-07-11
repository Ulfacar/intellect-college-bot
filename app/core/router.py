"""Определение воронки по тексту клиента (туры / виза / билеты).

MVP: простые ключевые слова. Дальше — классификация через LLM (cheap-модель).
"""
from __future__ import annotations

KEYWORDS = {
    "tours": ["тур", "отдых", "море", "отель", "путёвк", "путевк", "куда поехать"],
    "visa": ["виз", "шенген", "консульств", "посольств", "отказ"],
    "tickets": ["билет", "перелёт", "перелет", "авиа", "рейс"],
}


def detect_funnel(text: str) -> str | None:
    low = text.lower()
    for funnel, words in KEYWORDS.items():
        if any(w in low for w in words):
            return funnel
    return None

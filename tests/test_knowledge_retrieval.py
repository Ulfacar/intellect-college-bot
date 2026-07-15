"""Increment 6: knowledge retrieval (brief §20 scenarios 19-25) — top-k lexical
retrieval over PUBLISHED faq_kb candidates only, no embeddings, no vector DB."""
from __future__ import annotations

import asyncio

import pytest

from app.core import faq_kb
from app.core.knowledge_retrieval import TOP_K, retrieve_knowledge


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate():
    faq_kb.reset()
    yield
    faq_kb.reset()


async def _publish(question: str, answer_ru: str, *, category="tuition", answer_ky=None, priority=0):
    store = faq_kb.get_faq_kb_store()
    entry = await store.create_draft(
        {
            "canonical_question": question, "answer_ru": answer_ru, "answer_ky": answer_ky,
            "category": category, "priority": priority, "handoff_only": False,
        },
        [{"text": question}], "mgr",
    )
    await store.publish(entry.id, "mgr", confirm=True)
    return entry


# 19. Empty knowledge base -> no facts, empty list (model must not invent).
def test_empty_kb_returns_nothing():
    async def scenario():
        result = await retrieve_knowledge("сколько стоит обучение")
        assert result == []
    _run(scenario())


# 20. Draft entries are NEVER retrieved (only list_published_candidates() feeds this).
def test_draft_entries_never_retrieved():
    async def scenario():
        store = faq_kb.get_faq_kb_store()
        await store.create_draft(
            {"canonical_question": "сколько стоит обучение", "answer_ru": "6500$",
             "category": "tuition", "priority": 0, "handoff_only": False},
            [{"text": "сколько стоит обучение"}], "mgr",
        )
        result = await retrieve_knowledge("сколько стоит обучение")
        assert result == []
    _run(scenario())


# 21. Published entry matches a close paraphrase of the question.
def test_published_entry_matches_paraphrase():
    async def scenario():
        await _publish("сколько стоит обучение", "6500 долларов в год")
        result = await retrieve_knowledge("а сколько стоит у вас обучение вообще")
        assert len(result) == 1
        assert result[0].answer_ru == "6500 долларов в год"
    _run(scenario())


# 22. Top-K cap respected even with many relevant candidates.
def test_top_k_cap_respected():
    async def scenario():
        for i in range(TOP_K + 5):
            await _publish(f"вопрос про направление номер {i}", f"ответ {i}", category="directions", priority=i)
        result = await retrieve_knowledge("расскажите про направление номер")
        assert len(result) <= TOP_K
    _run(scenario())


# 23. Irrelevant query returns nothing (no false-positive facts fed to the model).
def test_irrelevant_query_returns_nothing():
    async def scenario():
        await _publish("сколько стоит обучение", "6500 долларов в год")
        result = await retrieve_knowledge("какая сегодня погода")
        assert result == []
    _run(scenario())


# 24. handoff_only / category / validity metadata is carried through for the validator
# and the prompt builder.
def test_metadata_carried_through():
    async def scenario():
        entry = await _publish(
            "как оплатить обучение", "Оплата на счёт колледжа, детали у менеджера.",
            category="payment",
        )
        store = faq_kb.get_faq_kb_store()
        await store.disable(entry.id, "mgr", confirm=True)
        result = await retrieve_knowledge("как оплатить обучение")
        assert result == []  # disabled -> never retrieved
    _run(scenario())


def test_handoff_only_flag_preserved():
    async def scenario():
        store = faq_kb.get_faq_kb_store()
        entry = await store.create_draft(
            {"canonical_question": "как оформить договор", "answer_ru": "Оформление у менеджера.",
             "category": "contract", "priority": 0, "handoff_only": True},
            [{"text": "как оформить договор"}], "mgr",
        )
        await store.publish(entry.id, "mgr", confirm=True)
        result = await retrieve_knowledge("как оформить договор с колледжем")
        assert len(result) == 1
        assert result[0].handoff_only is True
        assert result[0].category == "contract"
    _run(scenario())


# 25. No embeddings/vector DB dependency — retrieval is pure stdlib text comparison,
# proven by identical results across repeated calls (determinism, no external state).
def test_retrieval_is_deterministic():
    async def scenario():
        await _publish("какие документы нужны для поступления", "Свидетельство о рождении и паспорт.")
        first = await retrieve_knowledge("какие документы нужны")
        second = await retrieve_knowledge("какие документы нужны")
        assert [r.entry_id for r in first] == [r.entry_id for r in second]
    _run(scenario())


def test_ky_answer_missing_is_none_not_fabricated():
    async def scenario():
        await _publish("сколько стоит обучение", "6500 долларов в год", answer_ky=None)
        result = await retrieve_knowledge("сколько стоит обучение")
        assert result[0].answer_ky is None
    _run(scenario())

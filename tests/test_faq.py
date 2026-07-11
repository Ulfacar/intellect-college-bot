import asyncio

from app.core.branding import COLLEGE_ADDRESS
from app.core.faq import FaqEntryView, get_faq_store, match_faq, normalize_text, reset, seed_defaults


def _entry(**kw):
    data = {
        "id": 1,
        "funnel": None,
        "enabled": True,
        "priority": 0,
        "title": "rule",
        "patterns": ["admission price"],
        "negative_terms": [],
        "answer": "answer",
        "handoff_only": False,
        "allow_during_qualification": True,
    }
    data.update(kw)
    return FaqEntryView(**data)


def test_normalize_text_lowercases_yo_punctuation_and_spaces():
    assert normalize_text("  СКОЛЬКО, стоит поступление?!  Ёлка ") == "сколько стоит поступление елка"


def test_match_uses_highest_priority():
    low = _entry(id=1, priority=1, title="low", patterns=["сколько стоит"])
    high = _entry(id=2, priority=10, title="high", patterns=["сколько стоит"])

    assert match_faq("Сколько стоит обучение?", "admission", [low, high]) is high


def test_match_respects_negative_terms():
    rule = _entry(patterns=["цена"], negative_terms=["за год"], funnel="admission")

    assert match_faq("Какая цена обучения?", "admission", [rule]) is rule
    assert match_faq("Это цена за год?", "admission", [rule]) is None


def test_match_respects_funnel_scope_and_common_rules():
    scoped = _entry(id=1, funnel="admission", patterns=["адрес"], title="admission")
    common = _entry(id=2, funnel=None, patterns=["адрес"], priority=-1, title="common")

    assert match_faq("Напишите адрес", "admission", [scoped, common]) is scoped
    assert match_faq("Напишите адрес", None, [scoped, common]) is common


def test_match_skips_equal_priority_conflict():
    one = _entry(id=1, priority=5, patterns=["адрес"], title="one")
    two = _entry(id=2, priority=5, patterns=["адрес"], title="two")

    assert match_faq("адрес офиса", "admission", [one, two]) is None


def test_match_can_skip_during_qualification():
    rule = _entry(patterns=["адрес"], allow_during_qualification=False)

    assert match_faq("адрес офиса", "admission", [rule], pending_field="name") is None
    assert match_faq("адрес офиса", "admission", [rule]) is rule


def test_seed_defaults_adds_expected_rules_to_empty_memory_store():
    async def scenario():
        reset()
        await seed_defaults()
        rows = await get_faq_store().list(include_disabled=True)

        assert len(rows) == 11
        by_title = {row.title: row for row in rows}
        assert set(by_title) == {
            "Часы работы",
            "Адрес офиса",
            "Стоимость обучения",
            "Направления",
            "Вступительный тест",
            "Гарантии поступления",
            "Документы",
            "Дедлайн приёма",
            "После 9 или 11 класса",
            "Скидка",
            "Проходной балл",
        }
        assert COLLEGE_ADDRESS in by_title["Адрес офиса"].answer
        assert "6500" in by_title["Стоимость обучения"].answer
        assert by_title["Проходной балл"].handoff_only is True

    asyncio.run(scenario())


def test_seed_defaults_is_idempotent_for_non_empty_store():
    async def scenario():
        reset()
        store = get_faq_store()
        await seed_defaults()
        first = await store.list(include_disabled=True)
        await seed_defaults()
        second = await store.list(include_disabled=True)
        assert len(first) == 11
        assert len(second) == 11
        assert [row.id for row in second] == [row.id for row in first]

    asyncio.run(scenario())


def test_seeded_defaults_match_common_and_scoped_questions():
    async def scenario():
        reset()
        await seed_defaults()
        entries = await get_faq_store().candidates("admission")

        assert match_faq("во сколько вы работаете?", "admission", entries).title == "Часы работы"
        assert match_faq("сколько стоит обучение?", "admission", entries).title == "Стоимость обучения"
        assert match_faq("какие направления есть?", "admission", entries).title == "Направления"
        assert match_faq("какой проходной балл?", "admission", entries).title == "Проходной балл"
        assert match_faq("какие документы нужны?", "admission", entries).title == "Документы"

    asyncio.run(scenario())


def test_seed_defaults_adds_missing_rules_without_overwriting_manual_edits():
    async def scenario():
        reset()
        store = get_faq_store()
        manual = await store.upsert({
            "funnel": None,
            "enabled": True,
            "priority": 99,
            "title": "Часы работы",
            "patterns": ["ручной график"],
            "answer": "Ручной ответ",
        }, updated_by="admin")

        await seed_defaults()
        rows = await store.list(include_disabled=True)
        by_title = {row.title: row for row in rows}

        assert len(rows) == 11
        assert by_title["Часы работы"].id == manual.id
        assert by_title["Часы работы"].answer == "Ручной ответ"
        assert by_title["Стоимость обучения"].updated_by == "system:seed"

    asyncio.run(scenario())


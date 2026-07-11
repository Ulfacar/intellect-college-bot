import asyncio

from app.core.branding import (
    FRUNZE_DESTINATIONS,
    FRUNZE_OFFICE_ADDRESS,
    FRUNZE_WORKING_HOURS,
    GETVISA_WORKING_HOURS,
    VISA_SERVICE_PRICES,
)
from app.core.faq import FaqEntryView, get_faq_store, match_faq, normalize_text, reset, seed_defaults


def _entry(**kw):
    data = {
        "id": 1,
        "funnel": None,
        "enabled": True,
        "priority": 0,
        "title": "rule",
        "patterns": ["visa price"],
        "negative_terms": [],
        "answer": "answer",
        "handoff_only": False,
        "allow_during_qualification": True,
    }
    data.update(kw)
    return FaqEntryView(**data)


def test_normalize_text_lowercases_yo_punctuation_and_spaces():
    assert normalize_text("  СКОЛЬКО, стоит ВИЗА?!  Ёлка ") == "сколько стоит виза елка"


def test_match_uses_highest_priority():
    low = _entry(id=1, priority=1, title="low", patterns=["сколько стоит"])
    high = _entry(id=2, priority=10, title="high", patterns=["сколько стоит"])

    hit = match_faq("Сколько стоит виза?", "visa", [low, high])

    assert hit is high


def test_match_respects_negative_terms():
    rule = _entry(patterns=["цена"], negative_terms=["билета"], funnel="visa")

    assert match_faq("Какая цена визы?", "visa", [rule]) is rule
    assert match_faq("Какая цена билета?", "visa", [rule]) is None


def test_match_respects_funnel_scope_and_common_rules():
    visa = _entry(id=1, funnel="visa", patterns=["адрес"], title="visa")
    common = _entry(id=2, funnel=None, patterns=["адрес"], priority=-1, title="common")

    assert match_faq("Напишите адрес", "visa", [visa, common]) is visa
    assert match_faq("Напишите адрес", "tours", [visa, common]) is common


def test_match_skips_equal_priority_conflict():
    one = _entry(id=1, priority=5, patterns=["адрес"], title="one")
    two = _entry(id=2, priority=5, patterns=["адрес"], title="two")

    assert match_faq("адрес офиса", "visa", [one, two]) is None


def test_match_can_skip_during_qualification():
    rule = _entry(patterns=["адрес"], allow_during_qualification=False)

    assert match_faq("адрес офиса", "visa", [rule], pending_field="name") is None
    assert match_faq("адрес офиса", "visa", [rule]) is rule


def test_seed_defaults_adds_expected_rules_to_empty_memory_store():
    async def scenario():
        reset()
        await seed_defaults()

        rows = await get_faq_store().list(include_disabled=True)

        assert len(rows) == 10
        by_title = {row.title: row for row in rows}
        assert set(by_title) == {
            "Часы работы",
            "Адрес офиса",
            "Стоимость визовых услуг",
            "Направления туров",
            "Self-visa удержание",
            "Гарантии по визе",
            "Документы для визы США",
            "Отказ в визе",
            "Бронь и оплата тура",
            "Почему цена тура меняется",
        }
        assert FRUNZE_WORKING_HOURS in by_title["Часы работы"].answer
        assert GETVISA_WORKING_HOURS in by_title["Часы работы"].answer
        assert FRUNZE_OFFICE_ADDRESS in by_title["Адрес офиса"].answer
        assert "по какой стране" in by_title["Стоимость визовых услуг"].answer
        assert FRUNZE_DESTINATIONS in by_title["Направления туров"].answer
        assert all(row.allow_during_qualification for row in rows)
        assert not any(row.handoff_only for row in rows)

    asyncio.run(scenario())


def test_seed_defaults_is_idempotent_for_non_empty_store():
    async def scenario():
        reset()
        store = get_faq_store()
        await seed_defaults()
        first = await store.list(include_disabled=True)

        await seed_defaults()
        second = await store.list(include_disabled=True)

        assert len(first) == 10
        assert len(second) == 10
        assert [row.id for row in second] == [row.id for row in first]

    asyncio.run(scenario())


def test_seeded_defaults_match_common_and_scoped_questions():
    async def scenario():
        reset()
        await seed_defaults()
        store = get_faq_store()

        tours_entries = await store.candidates("tours")
        visa_entries = await store.candidates("visa")

        assert match_faq("во сколько вы работаете?", "tours", tours_entries).title == "Часы работы"
        assert match_faq("во сколько вы работаете?", "visa", visa_entries).title == "Часы работы"
        assert match_faq("сколько стоит виза в США?", "visa", visa_entries).title == "Стоимость визовых услуг"
        assert match_faq("сколько стоит тур", "tours", tours_entries) is None
        assert match_faq("сколько стоит тур", "visa", visa_entries) is None
        assert match_faq("я сам оформлю визу", "visa", visa_entries).title == "Self-visa удержание"
        assert match_faq("гарантируете визу?", "visa", visa_entries).title == "Гарантии по визе"
        assert match_faq("что нужно для брони тура?", "tours", tours_entries).title == "Бронь и оплата тура"

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

        assert len(rows) == 10
        assert by_title["Часы работы"].id == manual.id
        assert by_title["Часы работы"].answer == "Ручной ответ"
        assert by_title["Self-visa удержание"].updated_by == "system:seed"

    asyncio.run(scenario())

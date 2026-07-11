"""Валидатор исходящих реплик: авто-чинит безопасное, мягко флагует риски."""
from app.agent.validator import strip_markdown, validate_reply
from app.core.branding import PRICE_DISCLAIMER


def test_strip_markdown_removes_formatting():
    src = "**Ваш кейс рабочий**\n\n- Отказ по США\n- Нет истории\n# Итог"
    out = strip_markdown(src)
    assert "*" not in out
    assert "#" not in out
    assert "Ваш кейс рабочий" in out
    assert out.startswith("Ваш кейс рабочий")
    # буллеты превратились в обычные строки (без ведущего «- »)
    assert "- Отказ" not in out
    assert "Отказ по США" in out


def test_markdown_flagged_as_violation():
    clean, violations = validate_reply("**Привет**", "tours")
    assert clean == "Привет"
    assert "markdown" in violations


def test_spaced_long_dash_is_normalized():
    clean, violations = validate_reply("США — сопровождение 250$.", "visa")

    assert clean == "США. сопровождение 250$."
    assert "markdown" in violations


def test_tours_price_gets_disclaimer_appended():
    clean, violations = validate_reply("Отель 5*, 7 ночей — от 1000$", "tours")
    assert PRICE_DISCLAIMER in clean
    assert "tours_price_disclaimer_added" in violations


def test_tours_price_with_existing_disclaimer_not_doubled():
    text = "Отель 5* — от 1000$. Цена может меняться, уточним при бронировании."
    clean, violations = validate_reply(text, "tours")
    assert "tours_price_disclaimer_added" not in violations
    assert clean.count("может меня") == 1


def test_visa_price_not_flagged():
    # Заказчик разрешил называть официальный прайс визовых услуг → цена не нарушение.
    clean, violations = validate_reply("Сопровождение по визе США — 250$.", "visa")
    assert "price_in_no_price_funnel" not in violations
    assert "250$" in clean


def test_tickets_price_flagged_text_unchanged():
    # По билетам цену называет менеджер → сумму в реплике бота помечаем.
    clean, violations = validate_reply("Билет Бишкек–Стамбул стоит 300$.", "tickets")
    assert "price_in_no_price_funnel" in violations
    assert "300$" in clean  # текст не калечим, только флаг


def test_visa_guarantee_flagged():
    _, violations = validate_reply("Мы гарантируем визу, одобрят на 100%.", "visa")
    assert "possible_visa_guarantee" in violations


def test_visa_negated_guarantee_not_flagged():
    _, violations = validate_reply("Визу мы не гарантируем — решает консульство.", "visa")
    assert "possible_visa_guarantee" not in violations


def test_multiple_questions_flagged():
    _, violations = validate_reply("Как вас зовут? Куда планируете поездку?", "visa")
    assert "multiple_questions" in violations


def test_clean_reply_no_violations():
    clean, violations = validate_reply("Здравствуйте! В какую страну нужна виза?", "visa")
    assert violations == []
    assert clean == "Здравствуйте! В какую страну нужна виза?"


def test_visa_system_prompt_contains_service_prices():
    # Официальный прайс реально попадает в системный промпт визовой воронки.
    from app.agent.prompts.visa import SYSTEM

    assert "250$" in SYSTEM
    assert "100€" in SYSTEM

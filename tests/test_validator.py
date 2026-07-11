from app.agent.validator import strip_markdown, validate_reply


def test_strip_markdown_removes_formatting():
    src = "**Ваш кейс рабочий**\n\n- Отказ\n# Итог"
    out = strip_markdown(src)
    assert "*" not in out
    assert "#" not in out
    assert "- Отказ" not in out
    assert "Ваш кейс рабочий" in out


def test_markdown_flagged_as_violation():
    clean, violations = validate_reply("**Привет**", "admission")
    assert clean == "Привет"
    assert "markdown" in violations


def test_spaced_long_dash_is_normalized():
    clean, violations = validate_reply("Контракт — 6500 долларов.", "admission")
    assert clean == "Контракт. 6500 долларов."
    assert "markdown" in violations


def test_allowed_price_6500_not_flagged():
    _, violations = validate_reply("Стоимость по контракту 6500 долларов.", "admission")
    assert "admission_price_mismatch" not in violations


def test_other_price_flagged_but_text_unchanged():
    clean, violations = validate_reply("Стоимость 7000 долларов.", "admission")
    assert "admission_price_mismatch" in violations
    assert "7000" in clean


def test_admission_guarantee_flagged():
    _, violations = validate_reply("Гарантируем поступление на 100%.", "admission")
    assert "admission_guarantee" in violations


def test_admission_negated_guarantee_not_flagged():
    _, violations = validate_reply("Поступление не гарантируем.", "admission")
    assert "admission_guarantee" not in violations


def test_discount_percent_flagged():
    _, violations = validate_reply("Скидка будет 20%.", "admission")
    assert "admission_discount_amount" in violations


def test_discount_percent_with_dash_flagged():
    _, violations = validate_reply("Скидка — 20%", "admission")
    assert "admission_discount_amount" in violations


def test_passing_score_flagged():
    _, violations = validate_reply("Проходной балл 90.", "admission")
    assert "admission_passing_score" in violations


def test_passing_score_with_dash_flagged():
    _, violations = validate_reply("Проходной балл — 90", "admission")
    assert "admission_passing_score" in violations


def test_duration_3_years_flagged():
    _, violations = validate_reply("Учиться 3 года.", "admission")
    assert "admission_duration_claim" in violations


def test_multiple_questions_flagged():
    _, violations = validate_reply("Как вас зовут? После 9 или 11?", "admission")
    assert "multiple_questions" in violations


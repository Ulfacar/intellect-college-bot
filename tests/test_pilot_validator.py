"""Increment 6: BLOCKING validator (brief §20 scenarios 26-32) — every critical
violation must fail-closed; safe replies must pass through untouched (besides the
reused markdown-stripping)."""
from __future__ import annotations

from app.core.ai_schema import parse_ai_result
from app.core.knowledge_retrieval import RetrievedKnowledge
from app.core.pilot_validator import validate_ai_reply


def _classification(**overrides):
    base = {
        "intent": "asks_tuition", "confidence": 0.95, "evidence": "клиент спросил цену",
        "lead_temperature": "warm", "suggested_status": "info_sent", "next_action_type": None,
        "next_action_at": None, "should_handoff": False, "handoff_reason": None,
        "qualification_updates": {},
    }
    base.update(overrides)
    return base


def _ai_result(reply: str, *, facts_used=None, knowledge_entry_ids=None, classification_overrides=None):
    payload = {
        "reply": reply, "language": "ru",
        "answer_basis": {
            "knowledge_entry_ids": knowledge_entry_ids or [], "facts_used": facts_used or [],
        },
        "classification": _classification(**(classification_overrides or {})),
        "summary_update": "клиент спросил про стоимость",
        "safety": {"uncertain": False, "unsupported_claims": [], "requires_human_confirmation": False},
    }
    return parse_ai_result(payload)


def _knowledge(entry_id=1, category="tuition", answer_ru="Стоимость 6500 долларов в год.", handoff_only=False):
    return RetrievedKnowledge(
        entry_id=entry_id, category=category, answer_ru=answer_ru, answer_ky=None,
        handoff_only=handoff_only, score=0.9,
    )


# --------------------------------------------------------------------------------------
# Safe replies pass through.
# --------------------------------------------------------------------------------------

def test_safe_reply_with_correctly_sourced_fact_passes():
    knowledge = [_knowledge()]
    result = _ai_result(
        "Стоимость обучения 6500 долларов в год.",
        facts_used=[{"field": "tuition", "value": "6500 долларов в год", "source_entry_id": 1}],
        knowledge_entry_ids=[1],
    )
    outcome = validate_ai_reply(result, retrieved=knowledge)
    assert outcome.ok is True
    assert outcome.critical == []


def test_plain_greeting_passes():
    result = _ai_result("Здравствуйте! Расскажите, после какого класса поступаете?")
    outcome = validate_ai_reply(result, retrieved=[])
    assert outcome.ok is True


# 26. Fabricated price (mismatched number) -> critical.
def test_fabricated_price_is_critical():
    result = _ai_result("Стоимость обучения 9999 долларов в год.")
    outcome = validate_ai_reply(result, retrieved=[])
    assert outcome.ok is False
    assert "admission_price_mismatch" in outcome.critical


# 27. Unconfirmed discount amount -> critical.
def test_unconfirmed_discount_amount_is_critical():
    result = _ai_result("Мы дадим вам скидку 50% сразу.")
    outcome = validate_ai_reply(result, retrieved=[])
    assert outcome.ok is False
    assert "admission_discount_amount" in outcome.critical


# 28. Unconfirmed passing score -> critical.
def test_unconfirmed_passing_score_is_critical():
    result = _ai_result("Проходной балл в этом году 85.")
    outcome = validate_ai_reply(result, retrieved=[])
    assert outcome.ok is False
    assert "admission_passing_score" in outcome.critical


# 29. Admission guarantee -> critical.
def test_admission_guarantee_is_critical():
    result = _ai_result("Гарантируем 100% поступление в этом году.")
    outcome = validate_ai_reply(result, retrieved=[])
    assert outcome.ok is False
    assert "admission_guarantee" in outcome.critical


# Employment/result guarantees -> critical (new detectors, not in legacy validator.py).
def test_employment_guarantee_is_critical():
    result = _ai_result("Гарантируем 100% трудоустройство после выпуска.")
    outcome = validate_ai_reply(result, retrieved=[])
    assert outcome.ok is False
    assert "employment_guarantee" in outcome.critical


def test_result_guarantee_is_critical():
    result = _ai_result("Гарантируем, что вы точно сдадите вступительный тест.")
    outcome = validate_ai_reply(result, retrieved=[])
    assert outcome.ok is False
    assert "result_guarantee" in outcome.critical


# 30. Dangerous legal-contract claim -> critical.
def test_contract_legal_claim_is_critical():
    result = _ai_result("За расторжение договора вам грозит штраф и неустойка.")
    outcome = validate_ai_reply(result, retrieved=[])
    assert outcome.ok is False
    assert "contract_legal_claim" in outcome.critical


# 31. Value differs from cited source -> critical.
def test_value_differs_from_cited_source_is_critical():
    knowledge = [_knowledge(answer_ru="Стоимость 6500 долларов в год.")]
    result = _ai_result(
        "Обучение стоит 6500 долларов в год.",
        facts_used=[{"field": "tuition", "value": "7000 долларов", "source_entry_id": 1}],
        knowledge_entry_ids=[1],
    )
    outcome = validate_ai_reply(result, retrieved=knowledge)
    assert outcome.ok is False
    assert "fact_value_mismatch" in outcome.critical


def test_source_entry_id_not_in_retrieved_is_critical():
    result = _ai_result(
        "Стоимость обучения 6500 долларов в год.",
        facts_used=[{"field": "tuition", "value": "6500 долларов", "source_entry_id": 999}],
        knowledge_entry_ids=[999],
    )
    outcome = validate_ai_reply(result, retrieved=[])
    assert outcome.ok is False
    assert "fact_source_not_retrieved" in outcome.critical


# Sensitive fact mentioned in the reply with NO source_entry_id at all -> critical.
def test_sensitive_fact_without_any_source_is_critical():
    result = _ai_result("Оплата производится по договору с рассрочкой.")
    outcome = validate_ai_reply(result, retrieved=[])
    assert outcome.ok is False
    assert "sensitive_fact_without_source" in outcome.critical


# 32. Reference to a non-existent manager action -> critical (no real handoff/next_action).
def test_unconfirmed_manager_action_is_critical():
    result = _ai_result(
        "Хорошо, я перезвоню вам через 10 минут.",
        classification_overrides={"should_handoff": False, "next_action_type": None},
    )
    outcome = validate_ai_reply(result, retrieved=[])
    assert outcome.ok is False
    assert "unconfirmed_manager_action" in outcome.critical


def test_manager_action_with_real_handoff_is_not_flagged():
    result = _ai_result(
        "Хорошо, передам менеджеру, он свяжется с вами сегодня.",
        classification_overrides={"should_handoff": True, "handoff_reason": "callback"},
    )
    outcome = validate_ai_reply(result, retrieved=[])
    assert "unconfirmed_manager_action" not in outcome.critical


# Fable hardening §5: a fabricated tuition PERIOD is critical even when the number matches.
def test_fabricated_tuition_period_is_critical():
    knowledge = [_knowledge(answer_ru="Стоимость обучения 6500 долларов.")]  # NO period in source
    result = _ai_result(
        "Стоимость обучения 6500 долларов в год.",
        facts_used=[{"field": "tuition", "value": "6500 долларов", "source_entry_id": 1}],
        knowledge_entry_ids=[1],
    )
    outcome = validate_ai_reply(result, retrieved=knowledge)
    assert outcome.ok is False
    assert "tuition_period_fabricated" in outcome.critical


def test_tuition_price_without_period_is_allowed():
    knowledge = [_knowledge(answer_ru="Стоимость обучения 6500 долларов.")]  # NO period in source
    result = _ai_result(
        "Стоимость обучения 6500 долларов.",
        facts_used=[{"field": "tuition", "value": "6500 долларов", "source_entry_id": 1}],
        knowledge_entry_ids=[1],
    )
    outcome = validate_ai_reply(result, retrieved=knowledge)
    assert outcome.ok is True
    assert "tuition_period_fabricated" not in outcome.critical


def test_tuition_period_present_in_source_is_allowed():
    knowledge = [_knowledge(answer_ru="Стоимость обучения 6500 долларов в год.")]  # period IS in source
    result = _ai_result(
        "Стоимость обучения 6500 долларов в год.",
        facts_used=[{"field": "tuition", "value": "6500 долларов", "source_entry_id": 1}],
        knowledge_entry_ids=[1],
    )
    outcome = validate_ai_reply(result, retrieved=knowledge)
    assert outcome.ok is True


def test_ky_period_source_covers_ru_period_reply():
    knowledge = [RetrievedKnowledge(
        entry_id=1, category="tuition", answer_ru="Стоимость 6500 долларов",
        answer_ky="Окуу жылына 6500 доллар", handoff_only=False, score=0.9,
    )]
    result = _ai_result(
        "Стоимость обучения 6500 долларов в год.",
        facts_used=[{"field": "tuition", "value": "6500 долларов", "source_entry_id": 1}],
        knowledge_entry_ids=[1],
    )
    outcome = validate_ai_reply(result, retrieved=knowledge)
    assert outcome.ok is True  # KY "жылына" (year bucket) covers RU "в год"


def test_unrelated_period_phrase_not_flagged():
    """A period phrase in a NON-tuition reply (no price, no tuition source) is out of
    scope — never flagged."""
    result = _ai_result("Приём документов идёт круглый год, приходите когда удобно.")
    outcome = validate_ai_reply(result, retrieved=[])
    assert "tuition_period_fabricated" not in outcome.critical


# Informational-only violations never block (reused legacy contract: markdown/too_long).
def test_markdown_is_informational_not_blocking():
    result = _ai_result("**Здравствуйте!** Рады ответить на ваш вопрос.")
    outcome = validate_ai_reply(result, retrieved=[])
    assert outcome.ok is True
    assert "markdown" in outcome.violations
    assert "markdown" not in outcome.critical
    assert "**" not in outcome.clean_reply


def test_multiple_questions_is_informational_not_blocking():
    result = _ai_result("После какого класса поступаете? Как вас зовут?")
    outcome = validate_ai_reply(result, retrieved=[])
    assert outcome.ok is True
    assert "multiple_questions" in outcome.violations

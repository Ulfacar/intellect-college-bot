"""Increment 5: pure matcher tests (scenarios 11-20 of the brief's §20). No DB, no
store — `app.core.faq_matcher` is deliberately storage-independent (see its module
docstring), so these tests build `MatchCandidate` objects directly."""
from __future__ import annotations

from app.core.faq_matcher import MatchCandidate, VariantText, match, normalize_text


def _cand(entry_id, question, variants=(), *, answer_ru="ответ", answer_ky=None,
          category="general", priority=0, handoff_only=False):
    return MatchCandidate(
        faq_entry_id=entry_id, canonical_question=question,
        variants=[VariantText(i + 1, text) for i, text in enumerate(variants)],
        answer_ru=answer_ru, answer_ky=answer_ky, category=category, priority=priority,
        handoff_only=handoff_only,
    )


# --------------------------------------------------------------------------------------
# 11. Exact canonical_question match.
# --------------------------------------------------------------------------------------

def test_exact_canonical_match():
    cand = _cand(1, "Сколько стоит обучение?", answer_ru="6500 долларов")
    result = match("сколько стоит обучение", [cand], language="ru")
    assert result.matched is True
    assert result.match_type == "canonical"
    assert result.faq_entry_id == 1
    assert result.matched_variant_id is None
    assert result.answer == "6500 долларов"


# --------------------------------------------------------------------------------------
# 12. Exact variant match.
# --------------------------------------------------------------------------------------

def test_exact_variant_match():
    cand = _cand(1, "Стоимость обучения", variants=["цена обучения", "почём учёба"])
    result = match("почём учёба", [cand], language="ru")
    assert result.matched is True
    assert result.match_type == "variant"
    assert result.matched_variant_id == 2  # second variant in the list


# --------------------------------------------------------------------------------------
# 13. Normalized equality/substring (either direction).
# --------------------------------------------------------------------------------------

def test_normalized_substring_both_directions():
    cand = _cand(1, "документы для поступления")
    # query is a substring of the canonical text
    r1 = match("документы", [cand], language="ru")
    assert r1.matched and r1.match_type == "normalized"

    cand2 = _cand(2, "адрес")
    # canonical text is a substring of the (longer) query
    r2 = match("подскажите пожалуйста адрес колледжа", [cand2], language="ru")
    assert r2.matched and r2.match_type == "normalized"


# --------------------------------------------------------------------------------------
# 14. Conservative fuzzy: >=0.92 matches, below threshold does not.
# --------------------------------------------------------------------------------------

def test_fuzzy_threshold_boundary():
    cand = _cand(1, "какие документы нужны для поступления")
    # Small one-word typo — high similarity, should clear 0.92.
    close = match("какие документы нужны для поступление", [cand], language="ru")
    assert close.matched is True
    assert close.match_type == "fuzzy"
    assert close.score >= 0.92

    # Unrelated text — well below threshold, no match at all.
    far = match("во сколько открывается спортзал", [cand], language="ru")
    assert far.matched is False
    assert far.reason == "no_match"


# --------------------------------------------------------------------------------------
# 15. Tie between two DIFFERENT entries broken by priority.
# --------------------------------------------------------------------------------------

def test_tie_broken_by_priority():
    low = _cand(1, "расскажите про поступление", priority=1)
    high = _cand(2, "расскажите про поступление", priority=5)
    # Both canonical questions are identical -> exact-canonical tier ties; priority wins.
    result = match("расскажите про поступление", [low, high], language="ru")
    assert result.matched is True
    assert result.faq_entry_id == 2


# --------------------------------------------------------------------------------------
# 16. Equal priority tie -> ambiguous / no_match, NEVER picked randomly.
# --------------------------------------------------------------------------------------

def test_equal_priority_tie_is_ambiguous():
    a = _cand(1, "расскажите про поступление", priority=3)
    b = _cand(2, "расскажите про поступление", priority=3)
    for _ in range(5):  # repeat: must be deterministic, never "sometimes" pick one
        result = match("расскажите про поступление", [a, b], language="ru")
        assert result.matched is False
        assert result.reason == "ambiguous"


# --------------------------------------------------------------------------------------
# 17. No match on unrelated text.
# --------------------------------------------------------------------------------------

def test_no_match_on_unrelated_text():
    cand = _cand(1, "какие документы нужны")
    result = match("есть общежитие для иногородних студентов", [cand], language="ru")
    assert result.matched is False
    assert result.reason == "no_match"
    assert result.answer is None


# --------------------------------------------------------------------------------------
# 18. Canonical stage is preferred over variant stage even across different entries.
# --------------------------------------------------------------------------------------

def test_canonical_stage_preferred_over_variant_stage():
    canonical_hit = _cand(1, "сколько стоит обучение", priority=0)
    variant_hit = _cand(2, "другой вопрос", variants=["сколько стоит обучение"], priority=100)
    result = match("сколько стоит обучение", [canonical_hit, variant_hit], language="ru")
    assert result.matched is True
    assert result.match_type == "canonical"
    assert result.faq_entry_id == 1  # canonical tier wins even though variant_hit has higher priority


# --------------------------------------------------------------------------------------
# 19. Multiple variants on one entry — matched_variant_id identifies the exact hit.
# --------------------------------------------------------------------------------------

def test_matched_variant_id_identifies_exact_variant():
    cand = _cand(1, "стоимость обучения", variants=["почём учёба", "сколько стоит", "цена"])
    result = match("цена", [cand], language="ru")
    assert result.matched is True
    assert result.matched_variant_id == 3


# --------------------------------------------------------------------------------------
# 20. normalize_text: case/punctuation/ё-normalization; KY letters survive intact.
# --------------------------------------------------------------------------------------

def test_normalize_text_basics_and_ky_letters_preserved():
    assert normalize_text("Сколько СТОИТ, обучение?!") == normalize_text("сколько стоит обучение")
    assert normalize_text("Ещё вопрос") == normalize_text("еще вопрос")  # ё -> е
    assert normalize_text("  много   пробелов  ") == "много пробелов"
    ky_text = normalize_text("Окуу канча турат?")
    assert "ө" not in ky_text and "турат" in ky_text  # punctuation stripped, KY word intact

    cand = _cand(1, "Окуу канча турат?", answer_ky="6500 доллар")
    result = match("окуу канча турат", [cand], language="ky")
    assert result.matched is True
    assert result.answer == "6500 доллар"

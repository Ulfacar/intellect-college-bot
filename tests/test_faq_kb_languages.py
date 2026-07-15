"""Increment 5: RU/KY language handling (scenarios 21-25 of the brief's §20).
Deterministic detection (`detect_language`) + answer resolution (`match`) — NO LLM
anywhere in this module, per the design constraint."""
from __future__ import annotations

from app.core.faq_matcher import MatchCandidate, VariantText, detect_language, match


def _cand(entry_id=1, *, answer_ru="RU-ответ", answer_ky=None):
    return MatchCandidate(
        faq_entry_id=entry_id, canonical_question="сколько стоит обучение",
        variants=[VariantText(1, "окуу канча турат")],
        answer_ru=answer_ru, answer_ky=answer_ky, category="tuition", priority=0,
        handoff_only=False,
    )


# --------------------------------------------------------------------------------------
# 21. Kyrgyz-specific letters (ө/ү/ң) -> "ky".
# --------------------------------------------------------------------------------------

def test_detect_language_ky_letters():
    assert detect_language("Төлөм канча болот?") == "ky"    # ө
    assert detect_language("Үй-бүлө маалымат") == "ky"       # ү
    assert detect_language("Жаңылык бар бекен?") == "ky"     # ң
    assert detect_language("Түшүнбөй жатам") == "ky"         # ү + ө


# --------------------------------------------------------------------------------------
# 22. Plain Russian text, no stored language -> "ru" (default fallback).
# --------------------------------------------------------------------------------------

def test_detect_language_defaults_to_ru():
    assert detect_language("Сколько стоит обучение?") == "ru"
    assert detect_language("") == "ru"
    assert detect_language("123 !!!") == "ru"


def test_detect_language_uses_stored_language_when_ambiguous():
    # No KY letters, no strong RU signal either — falls back to the caller-provided
    # stored language hint (Conversation's known language), NOT an LLM guess.
    assert detect_language("123", stored_language="ky") == "ky"
    assert detect_language("123", stored_language="ru") == "ru"
    assert detect_language("123", stored_language=None) == "ru"


# --------------------------------------------------------------------------------------
# 23. RU query -> answer_ru, missing_answer_ky=False.
# --------------------------------------------------------------------------------------

def test_ru_query_uses_answer_ru():
    cand = _cand(answer_ru="Стоимость 6500$.", answer_ky="Баасы 6500$.")
    result = match("сколько стоит обучение", [cand], language="ru")
    assert result.matched is True
    assert result.answer == "Стоимость 6500$."
    assert result.missing_answer_ky is False


# --------------------------------------------------------------------------------------
# 24. KY query with answer_ky present -> answer_ky, missing_answer_ky=False.
# --------------------------------------------------------------------------------------

def test_ky_query_uses_answer_ky_when_present():
    cand = _cand(answer_ru="Стоимость 6500$.", answer_ky="Баасы 6500$.")
    result = match("окуу канча турат", [cand], language="ky")
    assert result.matched is True
    assert result.answer == "Баасы 6500$."
    assert result.missing_answer_ky is False


# --------------------------------------------------------------------------------------
# 25. KY query, answer_ky MISSING -> safe RU fallback + missing_answer_ky=True.
#     Never auto-translates. Playground/explicit `language` overrides detection.
# --------------------------------------------------------------------------------------

def test_ky_query_falls_back_to_ru_and_flags_missing():
    cand = _cand(answer_ru="Стоимость 6500$.", answer_ky=None)
    result = match("окуу канча турат", [cand], language="ky")
    assert result.matched is True
    assert result.answer == "Стоимость 6500$."   # safe RU fallback, NOT a translation
    assert result.missing_answer_ky is True
    assert result.language == "ky"


def test_explicit_language_param_overrides_detection():
    # Text reads as plain Russian, but an explicit "ky" language (e.g. playground
    # override / tester picking KY) forces the KY answer-resolution path anyway.
    cand = _cand(answer_ru="Стоимость 6500$.", answer_ky="Баасы 6500$.")
    forced_ky = match("сколько стоит обучение", [cand], language="ky")
    assert forced_ky.answer == "Баасы 6500$."
    assert forced_ky.language == "ky"

    forced_ru = match("сколько стоит обучение", [cand], language="ru")
    assert forced_ru.answer == "Стоимость 6500$."
    assert forced_ru.language == "ru"

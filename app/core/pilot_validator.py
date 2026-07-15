"""Increment 6: BLOCKING validator for the structured AI reply (fail-closed).

Reuses `app/agent/validator.py` detectors (`strip_markdown`, `validate_reply` and its
compiled regexes for admission price/discount/passing-score/guarantee/duration) exactly
as-is — `validator.py` itself is NOT modified and its "logging-only" contract for
`app/core/orchestrator.py` (legacy funnel path) is untouched; THIS module is a new,
separate, BLOCKING consumer of the same detectors for the Increment 6 pipeline only.

Pure function, no I/O, no storage/network — `app/core/ai_reply.py` calls
`validate_ai_reply(...)` and decides what to do with the result (send / fall back +
handoff / audit). This module only classifies violations as critical vs informational
and returns the already-markdown-stripped clean text.

Critical (ANY -> fail-closed, per the task brief):
  fabricated price / unconfirmed discount / unconfirmed passing_score / fabricated date
  / fabricated deadline / admission guarantee / discount guarantee / employment
  guarantee / result guarantee / a cited value that DIFFERS from its source / dangerous
  legal-contract claim / reference to a non-existent manager action / a sensitive fact
  mentioned with no matching `source_entry_id` among the retrieved knowledge.

Informational (from the reused legacy detectors — never blocks, matches
`validator.py`'s own contract): markdown (auto-stripped), too_long, multiple_questions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agent import validator as legacy_validator
from app.core.ai_schema import AiResult
from app.core.knowledge_retrieval import RetrievedKnowledge

SAFE_FALLBACK_TEXT = (
    "Чтобы не дать вам неточную информацию, я передам этот вопрос менеджеру приёмной комиссии."
)

# --------------------------------------------------------------------------------------
# New detectors (NOT in app/agent/validator.py — that module only covers admission
# price/discount/passing-score/guarantee/duration). Conservative on purpose: a false
# negative here just means the reply goes through (same risk profile as before this
# increment); a false positive silently degrades a correct answer to a handoff, so each
# pattern requires an explicit strong phrase, not a bare keyword.
# --------------------------------------------------------------------------------------

_EMPLOYMENT_GUARANTEE = re.compile(
    r"(100\s?%|гаранти\w*).{0,40}(трудоустрой\w*|работ\w+ найдёте|с работой)", re.IGNORECASE,
)
_RESULT_GUARANTEE = re.compile(
    r"(100\s?%|гаранти\w*).{0,40}(сдадите|пройдёте тест|результат\w*)", re.IGNORECASE,
)
_CONTRACT_LEGAL_CLAIM = re.compile(
    r"(штраф\w*|неустойк\w*|расторжени\w* договор\w*|юридическ\w* ответственност\w*)", re.IGNORECASE,
)
_MANAGER_ACTION_PROMISE = re.compile(
    r"(перезвон\w+|позвон\w+|свяж\w+ся|напиш\w+ вам).{0,20}"
    r"(через\s+\d+\s*(минут\w*|час\w*)|сегодня|завтра|в\s+\d{1,2}[:.]\d{2})",
    re.IGNORECASE,
)

_PAYMENT_KEYWORDS = re.compile(r"оплат\w*|рассроч\w*|реквизит\w*|перевод\w* на карт\w*", re.IGNORECASE)
_CONTRACT_KEYWORDS = re.compile(r"договор\w*", re.IGNORECASE)
_ENTRANCE_TEST_KEYWORDS = re.compile(r"вступительн\w*|экзамен\w*", re.IGNORECASE)
_DEADLINE_KEYWORDS = re.compile(
    r"дедлайн\w*|крайний срок|до какого числа|срок подачи|подать документы до", re.IGNORECASE,
)
_DISCOUNT_MENTION = re.compile(r"скидк\w*|арзандат\w*", re.IGNORECASE)

#: Tuition PERIOD qualifiers, bucketed by the period they assert. A price with a period
#: is a DIFFERENT (stronger) claim than a bare price — "6500" vs "6500 в год" vs "6500 за
#: курс" mean different things, and inventing the period is as much a fabrication as
#: inventing the number (Fable review, hardening §5). Bucketing (not raw string match)
#: lets a KY "жылына" source cover a RU "в год" reply and vice versa, while still
#: refusing a "в месяц"/"за курс" period the source never stated.
_TUITION_PERIOD_BUCKETS: dict[str, re.Pattern[str]] = {
    "year": re.compile(r"в год|за год|год\w* обучени|в году|жыл\w*", re.IGNORECASE),
    "month": re.compile(r"в месяц|за месяц|ай\w*ына", re.IGNORECASE),
    "course": re.compile(
        r"за курс|за весь курс|за (?:все|всё) обучени|за весь период|курс\w* обучени", re.IGNORECASE,
    ),
}

#: category -> regex used to decide "this reply mentions a fact of this sensitive
#: category" for the source cross-check below (§ knowledge_retrieval categories).
SENSITIVE_DETECTORS: dict[str, re.Pattern[str]] = {
    "tuition": legacy_validator._PRICE,
    "discounts": _DISCOUNT_MENTION,
    "payment": _PAYMENT_KEYWORDS,
    "entrance_test": _ENTRANCE_TEST_KEYWORDS,
    "passing_score": legacy_validator._PASSING_SCORE,
    "deadlines": _DEADLINE_KEYWORDS,
    "contract": _CONTRACT_KEYWORDS,
}

CRITICAL_VIOLATIONS: frozenset[str] = frozenset({
    "admission_guarantee", "admission_price_mismatch", "admission_discount_amount",
    "admission_passing_score", "admission_duration_claim",
    "employment_guarantee", "result_guarantee", "contract_legal_claim",
    "unconfirmed_manager_action", "fact_source_not_retrieved", "fact_value_mismatch",
    "sensitive_fact_without_source", "tuition_period_fabricated",
})
INFORMATIONAL_VIOLATIONS: frozenset[str] = frozenset({"markdown", "too_long", "multiple_questions"})


@dataclass
class ValidationOutcome:
    ok: bool
    clean_reply: str
    violations: list[str] = field(default_factory=list)
    critical: list[str] = field(default_factory=list)


def _detect_sensitive_categories(text: str) -> set[str]:
    return {category for category, pattern in SENSITIVE_DETECTORS.items() if pattern.search(text)}


def _tuition_period_buckets(text: str) -> set[str]:
    return {name for name, pattern in _TUITION_PERIOD_BUCKETS.items() if pattern.search(text)}


def _check_tuition_period(ai_result: AiResult, retrieved: list[RetrievedKnowledge]) -> set[str]:
    """Fail-closed if the reply attaches a tuition period qualifier (в год / за курс /
    жылына / ...) that NO cited tuition source actually states (Fable hardening §5). The
    numeric value is already covered by `fact_value_mismatch`; this closes the gap where
    the number matches but the period is invented. Only fires in a tuition context (a
    tuition source is cited, or a bare price appears) so an unrelated "в год" elsewhere
    is never flagged."""
    reply = ai_result.reply
    reply_buckets = _tuition_period_buckets(reply)
    if not reply_buckets:
        return set()

    retrieved_by_id = {item.entry_id: item for item in retrieved}
    tuition_sources = [
        retrieved_by_id[fact.source_entry_id]
        for fact in ai_result.answer_basis.facts_used
        if fact.source_entry_id in retrieved_by_id
        and retrieved_by_id[fact.source_entry_id].category == "tuition"
    ]
    has_price = bool(legacy_validator._PRICE.search(reply))
    if not tuition_sources and not has_price:
        return set()  # a period phrase, but not in a tuition context — out of scope

    source_buckets: set[str] = set()
    for src in tuition_sources:
        source_buckets |= _tuition_period_buckets(f"{src.answer_ru}\n{src.answer_ky or ''}")

    if reply_buckets - source_buckets:
        return {"tuition_period_fabricated"}
    return set()


def _check_sources(ai_result: AiResult, retrieved: list[RetrievedKnowledge]) -> set[str]:
    violations: set[str] = set()
    retrieved_by_id = {item.entry_id: item for item in retrieved}
    cited_categories: set[str] = set()

    for fact in ai_result.answer_basis.facts_used:
        source = retrieved_by_id.get(fact.source_entry_id)
        if source is None:
            violations.add("fact_source_not_retrieved")
            continue
        cited_categories.add(source.category)
        haystack = f"{source.answer_ru}\n{source.answer_ky or ''}"
        value = fact.value.strip()
        if value and value not in haystack:
            violations.add("fact_value_mismatch")

    mentioned = _detect_sensitive_categories(ai_result.reply)
    if mentioned - cited_categories:
        violations.add("sensitive_fact_without_source")
    return violations


def validate_ai_reply(ai_result: AiResult, *, retrieved: list[RetrievedKnowledge]) -> ValidationOutcome:
    """Run every detector; classify violations; return the markdown-stripped clean
    text (from the reused legacy `strip_markdown`) regardless of outcome — the caller
    only sends it when `ok` is True."""
    clean, legacy_violations = legacy_validator.validate_reply(ai_result.reply, "admission")
    violations: set[str] = set(legacy_violations)

    if _EMPLOYMENT_GUARANTEE.search(clean):
        violations.add("employment_guarantee")
    if _RESULT_GUARANTEE.search(clean):
        violations.add("result_guarantee")
    if _CONTRACT_LEGAL_CLAIM.search(clean):
        violations.add("contract_legal_claim")
    if _MANAGER_ACTION_PROMISE.search(clean) and not ai_result.classification.should_handoff \
            and not ai_result.classification.next_action_type:
        violations.add("unconfirmed_manager_action")

    violations |= _check_sources(ai_result, retrieved)
    violations |= _check_tuition_period(ai_result, retrieved)

    critical = sorted(v for v in violations if v in CRITICAL_VIOLATIONS)
    return ValidationOutcome(ok=not critical, clean_reply=clean, violations=sorted(violations), critical=critical)

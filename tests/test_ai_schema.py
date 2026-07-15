"""Increment 6: schema validation (brief §20 scenarios 14-18) — invalid JSON / unknown
enum / empty reply / oversized reply must all raise, never silently coerce. Also proves
`app/core/ai_schema.py` stays in lockstep with `app/integrations/panel/leadstore.py`'s
canonical 11 `lead_status` keys and with the hand-written tool schema in
`app/agent/structured_llm.py` (regression guard against the two enum lists drifting
apart)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.structured_llm import build_emit_response_tool
from app.core.ai_schema import (
    GRADE_BASES,
    INTENTS,
    LANGUAGES,
    LEAD_STATUSES,
    MAX_REPLY_LEN,
    AiResult,
    parse_ai_result,
)
from app.integrations.panel.leadstore import LEAD_STATUSES as LEADSTORE_LEAD_STATUSES


def _valid_payload(**overrides):
    payload = {
        "reply": "Здравствуйте! Обучение после 9 класса длится 2 года 10 месяцев.",
        "language": "ru",
        "answer_basis": {"knowledge_entry_ids": [], "facts_used": []},
        "classification": {
            "intent": "asks_general_info",
            "confidence": 0.95,
            "evidence": "клиент спросил про поступление",
            "lead_temperature": "warm",
            "suggested_status": "in_progress",
            "next_action_type": None,
            "next_action_at": None,
            "should_handoff": False,
            "handoff_reason": None,
            "qualification_updates": {},
        },
        "summary_update": "Клиент интересуется поступлением после 9 класса.",
        "safety": {"uncertain": False, "unsupported_claims": [], "requires_human_confirmation": False},
    }
    payload.update(overrides)
    return payload


# 14. Valid payload round-trips cleanly.
def test_valid_payload_parses():
    result = parse_ai_result(_valid_payload())
    assert result.reply.startswith("Здравствуйте")
    assert result.classification.intent == "asks_general_info"


# 15. Unknown intent -> ValidationError (never silently accepted).
def test_unknown_intent_rejected():
    payload = _valid_payload()
    payload["classification"]["intent"] = "made_up_intent"
    with pytest.raises(ValidationError):
        parse_ai_result(payload)


# 15b. Unknown suggested_status -> rejected.
def test_unknown_suggested_status_rejected():
    payload = _valid_payload()
    payload["classification"]["suggested_status"] = "not_a_real_status"
    with pytest.raises(ValidationError):
        parse_ai_result(payload)


# 15c. Unknown language -> rejected.
def test_unknown_language_rejected():
    payload = _valid_payload()
    payload["language"] = "english"
    with pytest.raises(ValidationError):
        parse_ai_result(payload)


# 15d. Invalid grade_base -> rejected (only 9/11/other/unknown).
def test_invalid_grade_base_rejected():
    payload = _valid_payload()
    payload["classification"]["qualification_updates"] = {"grade_base": "10"}
    with pytest.raises(ValidationError):
        parse_ai_result(payload)


# 16. Empty reply -> rejected.
def test_empty_reply_rejected():
    payload = _valid_payload(reply="")
    with pytest.raises(ValidationError):
        parse_ai_result(payload)


def test_blank_whitespace_reply_rejected():
    payload = _valid_payload(reply="   \n  ")
    with pytest.raises(ValidationError):
        parse_ai_result(payload)


# 17. Oversized reply -> rejected.
def test_oversized_reply_rejected():
    payload = _valid_payload(reply="x" * (MAX_REPLY_LEN + 1))
    with pytest.raises(ValidationError):
        parse_ai_result(payload)


def test_max_len_reply_accepted():
    payload = _valid_payload(reply="x" * MAX_REPLY_LEN)
    result = parse_ai_result(payload)
    assert len(result.reply) == MAX_REPLY_LEN


# 18. Extra/unknown top-level fields are rejected (extra="forbid") — catches a model
# that free-associates fields outside the contract instead of silently absorbing them.
def test_unknown_top_level_field_rejected():
    payload = _valid_payload()
    payload["unexpected_field"] = "surprise"
    with pytest.raises(ValidationError):
        parse_ai_result(payload)


def test_missing_required_field_rejected():
    payload = _valid_payload()
    del payload["classification"]
    with pytest.raises(ValidationError):
        parse_ai_result(payload)


def test_confidence_out_of_range_rejected():
    payload = _valid_payload()
    payload["classification"]["confidence"] = 1.5
    with pytest.raises(ValidationError):
        parse_ai_result(payload)


def test_invalid_next_action_at_dropped_not_rejected():
    """A malformed next_action_at with NO next_action_type is dropped (not fatal) —
    see Classification._next_action_requires_type. But a genuinely unparsable ISO
    string DOES still raise (caught earlier by the field validator)."""
    payload = _valid_payload()
    payload["classification"]["next_action_at"] = "not-a-date"
    with pytest.raises(ValidationError):
        parse_ai_result(payload)


def test_next_action_at_without_type_is_dropped():
    payload = _valid_payload()
    payload["classification"]["next_action_at"] = "2026-07-20T15:00:00+06:00"
    payload["classification"]["next_action_type"] = None
    result = parse_ai_result(payload)
    assert result.classification.next_action_at is None


# --------------------------------------------------------------------------------------
# Regression: enums never silently drift apart across modules.
# --------------------------------------------------------------------------------------

def test_lead_statuses_match_leadstore_canonical_set():
    assert LEAD_STATUSES == LEADSTORE_LEAD_STATUSES


def test_tool_schema_enums_match_ai_schema_enums():
    tool = build_emit_response_tool()
    props = tool["function"]["parameters"]["properties"]
    classification_props = props["classification"]["properties"]

    assert set(props["language"]["enum"]) == LANGUAGES
    assert set(classification_props["intent"]["enum"]) == INTENTS
    assert set(x for x in classification_props["suggested_status"]["enum"] if x is not None) == LEAD_STATUSES
    grade_base_enum = classification_props["qualification_updates"]["properties"]["grade_base"]["enum"]
    assert set(x for x in grade_base_enum if x is not None) == GRADE_BASES


def test_tool_schema_forces_single_required_call_shape():
    tool = build_emit_response_tool()
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "emit_response"
    required = tool["function"]["parameters"]["required"]
    for field in ("reply", "language", "answer_basis", "classification", "summary_update", "safety"):
        assert field in required


def test_ai_result_model_fields_cover_brief_shape():
    fields = set(AiResult.model_fields.keys())
    assert fields == {"reply", "language", "answer_basis", "classification", "summary_update", "safety"}

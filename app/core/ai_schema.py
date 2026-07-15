"""Increment 6: structured result schema for the single OpenRouter call.

One model call returns reply + classification + qualification in ONE JSON payload
(forced tool call `emit_response`, see `app/agent/structured_llm.py`). This module is
the single source of truth for that shape — the tool's JSON-schema parameters
(`app/agent/structured_llm.py::build_emit_response_tool`) are generated FROM the enums
declared here, and `app/core/ai_reply.py` validates every inbound payload against
`AiResult.model_validate(...)`.

Design rule (per task brief): "Validate ALL enums/nullables in plain code — invalid
JSON / unknown enum / empty reply / oversized reply -> NEVER send raw; safe fallback +
handoff." Pydantic `field_validator`s below ARE that plain code — a `ValidationError`
here is exactly the trigger `ai_reply.py` treats as a schema error (see
`app/core/ai_reply.py::_parse_ai_result`).

Nothing in this module makes network calls or touches storage.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------------------
# Enums (plain constants — no network dependency, importable by the prompt builder,
# the tool-schema builder and the validator, so all three stay in lockstep).
# --------------------------------------------------------------------------------------

LANGUAGES: frozenset[str] = frozenset({"ru", "ky", "mixed", "unknown"})

INTENTS: frozenset[str] = frozenset({
    "greeting", "asks_general_info", "asks_tuition", "asks_documents", "asks_direction",
    "asks_entrance_test", "asks_schedule", "provides_name", "provides_grade_base",
    "provides_direction", "callback_requested", "wants_to_visit", "wants_to_think",
    "objection_expensive", "compares_competitor", "explicit_rejection", "requests_manager",
    "complaint", "unsupported_question", "unclear",
})

LEAD_TEMPERATURES: frozenset[str] = frozenset({"new", "cold", "warm", "hot"})

# The 11 canonical lead_status keys (kept in sync with
# app/integrations/panel/leadstore.py::LEAD_STATUSES — duplicated here as a plain
# frozenset, not imported, so this module has zero storage-layer dependency; a
# regression test asserts the two sets stay equal).
LEAD_STATUSES: frozenset[str] = frozenset({
    "new", "pre_contract", "in_progress", "contract", "tested_thinking", "callback",
    "thinking", "invited", "info_sent", "rejected", "invalid_number",
})

NEXT_ACTION_TYPES: frozenset[str] = frozenset({"callback", "visit", "followup"})

GRADE_BASES: frozenset[str] = frozenset({"9", "11", "other", "unknown"})

MAX_REPLY_LEN = 900          # a bit above validator.MAX_LEN (600) — hard schema ceiling
MAX_SUMMARY_LEN = 1200
MAX_EVIDENCE_LEN = 400


class FactUsed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=200)
    source_entry_id: int


class AnswerBasis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knowledge_entry_ids: list[int] = Field(default_factory=list)
    facts_used: list[FactUsed] = Field(default_factory=list)


class QualificationUpdates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=160)
    grade_base: str | None = None
    direction: str | None = Field(default=None, max_length=160)

    @field_validator("grade_base")
    @classmethod
    def _check_grade_base(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in GRADE_BASES:
            raise ValueError(f"grade_base must be one of {sorted(GRADE_BASES)}, got {value!r}")
        return value

    @field_validator("name", "direction")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class Classification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(default="", max_length=MAX_EVIDENCE_LEN)
    lead_temperature: str
    suggested_status: str | None = None
    next_action_type: str | None = None
    next_action_at: str | None = None   # ISO 8601, validated as a string (parsed by caller)
    should_handoff: bool
    handoff_reason: str | None = Field(default=None, max_length=200)
    qualification_updates: QualificationUpdates = Field(default_factory=QualificationUpdates)

    @field_validator("intent")
    @classmethod
    def _check_intent(cls, value: str) -> str:
        if value not in INTENTS:
            raise ValueError(f"unknown intent {value!r}")
        return value

    @field_validator("lead_temperature")
    @classmethod
    def _check_temperature(cls, value: str) -> str:
        if value not in LEAD_TEMPERATURES:
            raise ValueError(f"unknown lead_temperature {value!r}")
        return value

    @field_validator("suggested_status")
    @classmethod
    def _check_suggested_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in LEAD_STATUSES:
            raise ValueError(f"unknown suggested_status {value!r}")
        return value

    @field_validator("next_action_type")
    @classmethod
    def _check_next_action_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in NEXT_ACTION_TYPES:
            raise ValueError(f"unknown next_action_type {value!r}")
        return value

    @field_validator("next_action_at")
    @classmethod
    def _check_next_action_at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from datetime import datetime
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"next_action_at is not a valid ISO datetime: {value!r}") from exc
        return parsed.isoformat()

    @model_validator(mode="after")
    def _next_action_requires_type(self) -> "Classification":
        if self.next_action_at and not self.next_action_type:
            # A bare date with no action type is not actionable — drop it rather than
            # reject the whole payload (schema error would be too aggressive here).
            self.next_action_at = None
        return self


class Safety(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uncertain: bool = False
    unsupported_claims: list[str] = Field(default_factory=list)
    requires_human_confirmation: bool = False


class AiResult(BaseModel):
    """The full `emit_response` tool-call payload — see module docstring."""

    model_config = ConfigDict(extra="forbid")

    reply: str = Field(min_length=1, max_length=MAX_REPLY_LEN)
    language: str
    answer_basis: AnswerBasis = Field(default_factory=AnswerBasis)
    classification: Classification
    summary_update: str = Field(default="", max_length=MAX_SUMMARY_LEN)
    safety: Safety = Field(default_factory=Safety)

    @field_validator("reply")
    @classmethod
    def _reply_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reply must not be blank")
        return value

    @field_validator("language")
    @classmethod
    def _check_language(cls, value: str) -> str:
        if value not in LANGUAGES:
            raise ValueError(f"unknown language {value!r}")
        return value


def parse_ai_result(payload: dict[str, Any]) -> AiResult:
    """Thin wrapper — raises `pydantic.ValidationError` on any schema violation
    (unknown enum, missing/blank required field, oversized text, ...). Callers must
    NEVER forward `payload` (or the raw model text) to the user on failure — see
    `app/core/ai_reply.py::_parse_ai_result` for the fail-closed handling contract."""
    return AiResult.model_validate(payload)

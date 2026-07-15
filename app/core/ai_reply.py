"""Increment 6 telegram-pilot: the single-call AI reply pipeline.

Replaces `orchestrator.handle(...)` for the LLM step of the Telegram pilot path (see
`app/core/telegram_commands.py::route_message` — `orchestrator.py` itself is NOT
modified and other channels still use it unchanged). One public entry point,
`generate_and_send_reply(...)`, runs the full pipeline:

    retrieve knowledge -> build prompt -> reserve budget -> call the structured LLM
    -> validate schema (app/core/ai_schema.py) -> run the BLOCKING validator
    (app/core/pilot_validator.py) -> re-check dialog ownership/takeover -> apply
    lead_status/qualification -> send -> log usage + answer-context
    (app/integrations/panel/ai_log_store.py).

Every failure mode is fail-closed: the raw model output is NEVER sent to the user (see
`app/core/ai_schema.py` and `app/core/pilot_validator.py` docstrings) — on any error the
client gets a short, honest fallback that never promises an action that wasn't actually
taken (no "I'll get back to you" unless a real handoff/next_action was created, per the
task brief's §13 rule).

`_llm_caller` is a module-level, monkeypatchable indirection to
`app.agent.structured_llm.call_structured` — tests inject a fake coroutine returning a
canned `StructuredCallResult` (or raising) instead of hitting the network; see
`tests/test_ai_reply_pipeline.py`.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.agent import structured_llm
from app.config import settings
from app.core import budget, knowledge_retrieval, pilot_prompt, pilot_validator
from app.core.ai_schema import parse_ai_result
from app.core.budget import reserve as reserve_budget
from app.core.conversation_service import ConversationService
from app.core.lead_status_service import MANAGER_ONLY, LeadStatusService
from app.integrations.panel.ai_log_store import get_ai_log_store
from app.integrations.panel.audit_store import get_audit_store
from app.integrations.panel.leadstore import get_lead_store
from app.integrations.panel.store import get_conversation_store

log = logging.getLogger("ai_reply")

HISTORY_MESSAGE_LIMIT = 14
HISTORY_CHAR_CAP = 6000
SUMMARY_MAX_LEN = 1200

TECHNICAL_ERROR_FALLBACK = (
    "Не получилось обработать ваш вопрос — повторите, пожалуйста, чуть позже, или "
    "напишите /manager, чтобы позвать менеджера приёмной комиссии."
)
BUDGET_EXHAUSTED_FALLBACK = (
    "Сейчас не могу ответить сразу. Напишите, пожалуйста, чуть позже — или отправьте "
    "/manager, и вам ответит менеджер приёмной комиссии."
)

# Approximate fallback rate ONLY (used when the provider response has no usage.cost —
# see app/agent/structured_llm.py::_extract_usage). Reuses the SAME verified OpenRouter
# rate constants app/core/budget.py uses for its worst-case reservation estimate ($1/$5
# per 1M input/output tokens for anthropic/claude-haiku-4.5 at increment time). NEVER
# used to gate the real per-request cost recorded when the provider reports one.
_ESTIMATED_RATE_INPUT_PER_1M = budget.RESERVATION_RATE_INPUT_PER_1M
_ESTIMATED_RATE_OUTPUT_PER_1M = budget.RESERVATION_RATE_OUTPUT_PER_1M

_OUTCOME_FOR_ERROR: dict[str, str] = {
    "no_api_key": "no_api_key",
    "timeout": "timeout",
    "connection": "connection",
    "http_429": "http_error",
    "http_5xx": "http_error",
    "http_4xx": "http_error",
    "unauthorized": "unauthorized",
    "payment_required": "payment_required",
    "invalid_json": "schema_error",
    "no_tool_call": "schema_error",
}
_SCHEMA_ERROR_OUTCOMES = frozenset({"schema_error"})

# Swappable indirection for tests — see module docstring.
_llm_caller = structured_llm.call_structured


def _key(bot_id: str, user_id: str) -> str:
    return f"{bot_id}:{user_id}" if bot_id else user_id


def _extract_client_message_id(msg: Any) -> str | None:
    raw_msg = (getattr(msg, "raw", None) or {}).get("message") or {}
    mid = raw_msg.get("message_id")
    return str(mid) if mid is not None else None


async def _build_history_messages(key: str) -> list[dict[str, str]]:
    """Last `HISTORY_MESSAGE_LIMIT` panel messages (already includes the current
    incoming turn — it is logged BEFORE this pipeline runs, see
    `app/core/telegram_commands.py::route_message`), trimmed to `HISTORY_CHAR_CAP`
    total characters from the newest backwards. Fails open to `[]` on any panel-read
    error — an empty history just means a colder-context reply, never a crash."""
    try:
        conv_view = await get_conversation_store().get(key)
    except Exception:  # noqa: BLE001
        log.warning("ai_reply history read failed (key=%s)", key, exc_info=True)
        return []
    if conv_view is None:
        return []
    recent = conv_view.messages[-HISTORY_MESSAGE_LIMIT:]
    buffer: list[dict[str, str]] = []
    total_chars = 0
    for item in reversed(recent):
        text = item.text or ""
        if total_chars + len(text) > HISTORY_CHAR_CAP:
            break
        total_chars += len(text)
        role = "user" if item.sender == "client" else "assistant"
        buffer.append({"role": role, "content": text})
    buffer.reverse()
    return buffer


async def _send_and_log(*, msg: Any, bot_id: str, adapter: Any, text: str) -> str | None:
    """Send + log through the legacy panel — same pending -> sent/failed lifecycle as
    `Orchestrator._reply`/`telegram_commands._send_faq_reply`. Returns the panel
    message id (as `bot_message_id` for `ai_answer_log`) or `None`."""
    panel = get_conversation_store()
    key = _key(bot_id, msg.user_id)
    msg_id = 0
    try:
        msg_id = await panel.add_message(
            key, "bot", text, channel=msg.channel, bot_id=bot_id, status="pending", phone=msg.user_id,
        )
    except Exception:  # noqa: BLE001
        log.warning("ai_reply panel log_out failed (bot=%s)", bot_id, exc_info=True)
    try:
        provider_msg_id = await adapter.send(msg.chat_id, text)
        if msg_id:
            await panel.mark_message_status(
                message_id=msg_id, status="sent", set_provider_msg_id=(provider_msg_id or None),
            )
    except Exception:  # noqa: BLE001 — channel failure: mark failed, never crash the pipeline
        if msg_id:
            try:
                await panel.mark_message_status(message_id=msg_id, status="failed")
            except Exception:  # noqa: BLE001
                pass
        log.warning("ai_reply send failed (bot=%s)", bot_id, exc_info=True)
    return str(msg_id) if msg_id else None


async def _request_handoff(conversation_id: int, *, reason: str) -> None:
    try:
        await ConversationService().request_manager(conversation_id, actor="ai", reason=reason[:200])
    except Exception:  # noqa: BLE001 — never let a handoff-audit failure break the reply flow
        log.warning("ai_reply request_manager failed (conversation_id=%s)", conversation_id, exc_info=True)


async def _apply_qualification_updates(lead: Any, classification: Any, *, threshold: float) -> None:
    """§11: only `name`/`grade_base`/`direction` (schema-level fields) +
    `next_action_type`/`next_action_at` (top-level classification fields, only when
    safely parsed). Never overwrites an already-confirmed (truthy) field unless the
    model's overall confidence clears `threshold` — a low-confidence guess can fill a
    gap but never silently replace a previously confirmed value."""
    if lead is None:
        return
    updates = classification.qualification_updates
    fields: dict[str, Any] = {}
    high_confidence = classification.confidence >= threshold

    if updates.name and (not lead.name or high_confidence):
        fields["name"] = updates.name
    if updates.grade_base and (not lead.grade_base or high_confidence):
        fields["grade_base"] = updates.grade_base
    if updates.direction and (not lead.direction or high_confidence):
        fields["direction"] = updates.direction

    if classification.next_action_type and classification.next_action_at:
        try:
            parsed_at = datetime.fromisoformat(classification.next_action_at)
        except ValueError:
            parsed_at = None
        if parsed_at is not None and (not lead.next_action_at or high_confidence):
            fields["next_action_type"] = classification.next_action_type
            fields["next_action_at"] = parsed_at

    if fields:
        await get_lead_store().update_lead(lead.id, **fields)


async def _apply_classification(lead: Any, conv: Any, classification: Any) -> str | None:
    """Applies `classification.suggested_status` via `LeadStatusService`, respecting
    `settings.ai_status_confidence_threshold` and the manager-only status list. The
    suggestion itself is ALWAYS persisted onto `Lead.suggested_status` first (whether
    or not the actual transition is allowed/attempted) — the model only ever SUGGESTS,
    per §10; `LeadStatusService` is the sole decision-maker for the real `lead_status`.
    Returns the status that actually got APPLIED (or `None` if none did)."""
    if lead is None:
        return None

    threshold = settings.ai_status_confidence_threshold
    await _apply_qualification_updates(lead, classification, threshold=threshold)

    target = classification.suggested_status
    if target is None:
        return None

    await get_lead_store().update_lead(lead.id, suggested_status=target)

    if classification.confidence < threshold or target in MANAGER_ONLY:
        return None

    service = LeadStatusService()
    if classification.intent == "wants_to_visit" and target == "invited":
        result = await service.apply_invited_handoff(
            lead.id, conv.id, actor="ai", reason=classification.evidence[:200] or None,
            confidence=classification.confidence,
        )
    else:
        result = await service.set_status(
            lead.id, target, source="bot", actor="ai", reason=classification.evidence[:200] or None,
            confidence=classification.confidence, conversation_id=conv.id, suggested_status=target,
        )
    return result.current_status if result.changed else None


def _estimate_cost(usage: structured_llm.UsageInfo) -> float:
    return (
        (usage.input_tokens / 1_000_000) * _ESTIMATED_RATE_INPUT_PER_1M
        + (usage.output_tokens / 1_000_000) * _ESTIMATED_RATE_OUTPUT_PER_1M
    )


async def _finalize_error(log_id: int, *, outcome: str, call_result: Any) -> None:
    try:
        await get_ai_log_store().finalize(
            log_id, outcome=outcome, latency_ms=call_result.latency_ms,
            retry_count=call_result.retry_count, model=call_result.model,
        )
    except Exception:  # noqa: BLE001 — logging must never break the fallback already sent
        log.warning("ai_reply finalize (error path) failed", exc_info=True)


async def generate_and_send_reply(
    msg: Any, *, bot_id: str, adapter: Any, orchestrator: Any, session: Any,
) -> str:
    """Full pipeline for one already-gated Telegram message (private, allowlisted,
    active session, bot-owned, effective-on, no managed-FAQ match, not non-text, not
    already budget-gated by the caller). Returns an outcome code (also the
    `ai_answer_log.outcome` value) for tests/observability — callers do not need to
    branch on it, the pipeline sends everything it needs to send."""
    conv = session.conversation
    lead = session.lead
    key = _key(bot_id, msg.user_id)
    request_id = uuid4().hex
    model = settings.llm_model_main

    retrieved = await knowledge_retrieval.retrieve_knowledge(msg.text or "")

    bot_cfg = getattr(orchestrator, "bot", None)
    system = pilot_prompt.build_system_prompt(
        bot=bot_cfg, retrieved=retrieved, qualification=(lead.qualification if lead else {}),
        bot_phase=conv.bot_phase, lead_status=(lead.lead_status if lead else "new"),
        dialog_owner=conv.dialog_owner, ai_summary=(lead.ai_summary if lead else None),
    )
    messages = await _build_history_messages(key)
    if not messages:
        messages = [{"role": "user", "content": msg.text or ""}]

    reservation = await reserve_budget(
        bot_id=bot_id, conversation_id=conv.id, lead_id=(lead.id if lead else None),
        model=model, prompt_version=pilot_prompt.PROMPT_VERSION, request_id=request_id,
    )
    if not reservation.allowed:
        await _send_and_log(msg=msg, bot_id=bot_id, adapter=adapter, text=BUDGET_EXHAUSTED_FALLBACK)
        return "budget_exhausted"

    log_id = reservation.log_id
    assert log_id is not None

    call_result = await _llm_caller(
        system=system, messages=messages, model=model,
        max_output_tokens=settings.llm_max_output_tokens,
        timeout_seconds=settings.llm_request_timeout_seconds,
    )

    if not call_result.ok:
        outcome = _OUTCOME_FOR_ERROR.get(call_result.error or "", "error")
        await _finalize_error(log_id, outcome=outcome, call_result=call_result)
        await _send_and_log(msg=msg, bot_id=bot_id, adapter=adapter, text=TECHNICAL_ERROR_FALLBACK)
        if outcome in _SCHEMA_ERROR_OUTCOMES and conv.id is not None:
            await _request_handoff(conv.id, reason=f"ai_error:{call_result.error}")
        return outcome

    try:
        ai_result = parse_ai_result(call_result.arguments or {})
    except ValidationError:
        await _finalize_error(log_id, outcome="schema_error", call_result=call_result)
        await _send_and_log(msg=msg, bot_id=bot_id, adapter=adapter, text=pilot_validator.SAFE_FALLBACK_TEXT)
        await _request_handoff(conv.id, reason="ai_schema_validation_error")
        return "schema_error"

    validation = pilot_validator.validate_ai_reply(ai_result, retrieved=retrieved)
    if not validation.ok:
        await _finalize_error(log_id, outcome="validator_blocked", call_result=call_result)
        try:
            await get_ai_log_store().finalize(log_id, validator_violations=validation.critical)
        except Exception:  # noqa: BLE001
            log.warning("ai_reply validator-violation log write failed", exc_info=True)
        try:
            await get_audit_store().record(
                lead_id=(lead.id if lead else None), conversation_id=conv.id,
                event_type="ai_validator_blocked", source="system", actor="ai_validator",
                reason=",".join(validation.critical), metadata={"violations": validation.critical},
            )
        except Exception:  # noqa: BLE001
            log.warning("ai_reply validator audit write failed", exc_info=True)
        await _send_and_log(msg=msg, bot_id=bot_id, adapter=adapter, text=pilot_validator.SAFE_FALLBACK_TEXT)
        await _request_handoff(conv.id, reason="validator_blocked")
        return "validator_blocked"

    # §16 takeover re-check — AFTER validation, BEFORE applying status or sending.
    fresh_conv = await get_lead_store().get_conversation(conv.id)
    bots_on_check = getattr(orchestrator, "_bots_on", None)
    effective_on = True if bots_on_check is None else await bots_on_check()
    takeover = (
        fresh_conv is None
        or fresh_conv.dialog_owner != "bot"
        or fresh_conv.session_id != conv.session_id
        or fresh_conv.archived_at is not None
        or not effective_on
    )
    if takeover:
        await _finalize_error(log_id, outcome="cancelled_by_takeover", call_result=call_result)
        return "cancelled_by_takeover"

    applied_status = await _apply_classification(lead, conv, ai_result.classification)

    if lead is not None and ai_result.summary_update.strip():
        await get_lead_store().update_lead(
            lead.id, ai_summary=ai_result.summary_update.strip()[:SUMMARY_MAX_LEN],
        )

    bot_message_id = await _send_and_log(msg=msg, bot_id=bot_id, adapter=adapter, text=validation.clean_reply)

    if ai_result.classification.should_handoff:
        await _request_handoff(
            conv.id, reason=ai_result.classification.handoff_reason or ai_result.classification.intent,
        )

    usage = call_result.usage or structured_llm.UsageInfo()
    cost = usage.cost if usage.cost is not None else _estimate_cost(usage)
    try:
        await get_ai_log_store().finalize(
            log_id, outcome="sent", latency_ms=call_result.latency_ms, retry_count=call_result.retry_count,
            model=call_result.model, generation_id=call_result.generation_id,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens, cached_tokens=usage.cached_tokens,
            cost=cost, cost_source=usage.cost_source,
            client_message_id=_extract_client_message_id(msg), bot_message_id=bot_message_id,
            knowledge_entry_ids=[item.entry_id for item in retrieved], language=ai_result.language,
            intent=ai_result.classification.intent, confidence=ai_result.classification.confidence,
            evidence=ai_result.classification.evidence, suggested_status=ai_result.classification.suggested_status,
            applied_status=applied_status, lead_temperature=ai_result.classification.lead_temperature,
            bot_phase=fresh_conv.bot_phase, dialog_owner=fresh_conv.dialog_owner,
        )
    except Exception:  # noqa: BLE001 — the reply was already sent; logging must not raise
        log.warning("ai_reply final usage/context log write failed", exc_info=True)

    return "sent"

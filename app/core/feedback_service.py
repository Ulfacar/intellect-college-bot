"""Increment 7 telegram-pilot: FeedbackService — ALL business logic for the unified
AnswerContext + Feedback model. Every automatic-answer send point (FAQ/handoff in
`app/core/telegram_commands.py`, LLM/fallbacks in `app/core/ai_reply.py`) calls
`prepare_answer(...)`/`finalize_answer(...)` here to create the `answer_context` row
and (conditionally) build the inline-keyboard `reply_markup`. The Telegram callback
handler (`app/main.py`) is thin — it only extracts `callback_query` fields and calls
`handle_callback(...)`; every decision (authorization, idempotency, audit) lives here.

Design docs: task brief §5-§18 (unified AnswerContext, inline feedback buttons,
callback authorization, idempotency, pending-comment state, `/feedback` command,
review-backend methods, transactions/ack). See also
`app/integrations/panel/answer_context_store.py` and
`app/integrations/panel/feedback_store.py` for the storage contracts this composes.

Callback authorization (§7) is re-checked from scratch on EVERY callback — a callback
is NEVER trusted just because it came from Telegram. On ANY violation: no Feedback
row is created/changed, OpenRouter/FAQ are never called, the callback is acked with a
short neutral message, and a technical audit event is written with NO extra data
(no raw callback text, no PII — see `_reject`).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.core import allowlist
from app.integrations.panel.answer_context_store import (
    FEEDBACK_ELIGIBLE_SOURCES,
    AnswerContextView,
    get_answer_context_store,
)
from app.integrations.panel.audit_store import get_audit_store
from app.integrations.panel.feedback_store import RATINGS, FeedbackView, get_feedback_store
from app.integrations.panel.leadstore import get_lead_store

log = logging.getLogger("feedback_service")

# --------------------------------------------------------------------------------------
# Inline keyboard layout (§5) — codes kept short so `fb:<token>:<code>` stays well under
# the 64-byte callback_data limit (token ~12 chars + longest code "nopush" = 6 -> 22
# bytes total). NEVER put text/phone/history/secret/long UUID/JSON in callback_data.
# --------------------------------------------------------------------------------------

CODE_TO_RATING: dict[str, str] = {
    "ok": "correct", "inacc": "inaccurate", "bad": "incorrect",
    "push": "should_push", "nopush": "should_not_push", "mgr": "should_handoff",
}
COMMENT_CODE = "cmt"
assert set(CODE_TO_RATING.values()) == RATINGS  # keep the two enums in sync

# Human RU labels echoed back in the callback ack so the tester sees WHICH of the small
# buttons registered (Fable UX review #2). These are display labels only — the stored
# `rating` value and its callback `code` are unchanged. The 👤 label is intentionally
# past tense ("Нужен был менеджер") to read as a judgement about THIS reply and avoid
# colliding with the live `/manager` command (Fable UX review #4).
RATING_LABELS: dict[str, str] = {
    "correct": "Правильно",
    "inaccurate": "Неточно",
    "incorrect": "Неправильно",
    "should_push": "Нужно дожать",
    "should_not_push": "Не дожимать",
    "should_handoff": "Нужен был менеджер",
}
assert set(RATING_LABELS) == RATINGS  # every rating has a label


def _first_rating_text(rating: str) -> str:
    return f"Оценка сохранена: {RATING_LABELS[rating]}."


def _updated_rating_text(rating: str) -> str:
    return f"Оценка заменена — по ответу учитывается только одна оценка: {RATING_LABELS[rating]}."


_NEUTRAL_REJECT_TEXT = "Действие недоступно."
# Short (plain, non-alert) callback ack shown on the button spinner when 💬 is pressed —
# the SUBSTANTIVE, persistent instruction is the chat message below (Fable UX review #1),
# because a show_alert popup vanishes and leaves comment mode invisible.
_COMMENT_ACK_TEXT = "Режим комментария включён."
# Persistent chat message sent (via the adapter, NOT logged to the panel and NOT fed to
# the LLM history — §16) so the tester can SEE they are in comment mode and how to exit.
COMMENT_MODE_MESSAGE = (
    "✍️ Жду комментарий к этому ответу. Следующее ваше сообщение будет сохранено как "
    "комментарий, а не отправлено боту. Отмена — /cancel."
)
COMMENT_SAVED_TEXT = "Комментарий сохранён."
COMMENT_TARGET_MISSING_TEXT = "Не удалось сохранить комментарий — ответ больше не найден."
CANCEL_CLEARED_TEXT = "Комментарий отменён."
CANCEL_NOTHING_TEXT = "Режим комментария не был включён — отменять нечего."
NO_AUTOMATIC_ANSWER_TEXT = "Нет ответа бота, к которому можно привязать комментарий."


def _btn(text: str, feedback_token: str, code: str) -> dict[str, str]:
    return {"text": text, "callback_data": f"fb:{feedback_token}:{code}"}


def build_feedback_keyboard(feedback_token: str) -> dict[str, Any]:
    """Plain Bot-API-shaped `reply_markup` dict (never an aiogram type here — the
    adapter, not business logic, knows about aiogram, see `app/channels/telegram.py`).
    Phone-friendly row layout (Fable UX review #3 — no button dropped, callback_data
    unchanged):
      [✅ Правильно | ⚠️ Неточно | ❌ Неправильно]
      [🔥 Нужно дожать | 🧊 Не дожимать]
      [👤 Нужен был менеджер | 💬 Комментарий]
    """
    return {
        "inline_keyboard": [
            [
                _btn("✅ Правильно", feedback_token, "ok"),
                _btn("⚠️ Неточно", feedback_token, "inacc"),
                _btn("❌ Неправильно", feedback_token, "bad"),
            ],
            [
                _btn("🔥 Нужно дожать", feedback_token, "push"),
                _btn("🧊 Не дожимать", feedback_token, "nopush"),
            ],
            [
                _btn("👤 Нужен был менеджер", feedback_token, "mgr"),
                _btn("💬 Комментарий", feedback_token, COMMENT_CODE),
            ],
        ]
    }


def _parse_callback_data(data: str) -> tuple[str, str] | None:
    if not data or not data.startswith("fb:"):
        return None
    parts = data.split(":", 2)
    if len(parts) != 3:
        return None
    _, token, code = parts
    if not token or not code:
        return None
    return token, code


# --------------------------------------------------------------------------------------
# Pending-comment state (§9) — keyed by (bot_id, telegram_tester_id, session_id), NOT a
# global flag: two different testers (or the same tester across two sessions) never
# collide. Ephemeral, process-local (same single-process/sticky-session convention as
# `app/core/telegram_sessions.py::_locks`) — a restart just means an in-flight "press
# comment, then type it" flow has to be redone, which is an acceptable pilot trade-off.
# --------------------------------------------------------------------------------------

PENDING_COMMENT_TTL_SECONDS = 12 * 60  # within the documented 10-15 min window

_pending_comments: dict[tuple[str, str, str], tuple[int, datetime]] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pending_key(bot_id: str, tester_id: Any, session_id: str) -> tuple[str, str, str]:
    return (bot_id, str(tester_id), session_id)


def set_pending_comment(bot_id: str, tester_id: Any, session_id: str, answer_context_id: int) -> None:
    _pending_comments[_pending_key(bot_id, tester_id, session_id)] = (
        answer_context_id, _now() + timedelta(seconds=PENDING_COMMENT_TTL_SECONDS),
    )


def get_pending_comment_target(bot_id: str, tester_id: Any, session_id: str) -> int | None:
    key = _pending_key(bot_id, tester_id, session_id)
    entry = _pending_comments.get(key)
    if entry is None:
        return None
    answer_context_id, expires_at = entry
    if _now() > expires_at:
        _pending_comments.pop(key, None)
        return None
    return answer_context_id


def clear_pending_comment(bot_id: str, tester_id: Any, session_id: str) -> bool:
    """Returns True iff something was actually cleared (used by `/cancel`'s reply)."""
    return _pending_comments.pop(_pending_key(bot_id, tester_id, session_id), None) is not None


def reset_pending_comments() -> None:
    """Test-only: clear all in-memory pending-comment state between tests."""
    _pending_comments.clear()


# --------------------------------------------------------------------------------------
# FeedbackService
# --------------------------------------------------------------------------------------

@dataclass
class PreparedAnswer:
    """Returned by `prepare_answer` — the caller sends the reply with
    `reply_markup` (may be `None`, e.g. feedback disabled/tester not allowlisted) and
    then MUST call `finalize_answer(context_id, ...)` with the resulting message ids."""

    context_id: int
    feedback_token: str
    reply_markup: dict[str, Any] | None


class FeedbackService:
    def __init__(self, *, answer_context_store=None, feedback_store=None, audit_store=None, lead_store=None) -> None:
        self._ctx_store = answer_context_store or get_answer_context_store()
        self._fb_store = feedback_store or get_feedback_store()
        self._audit_store = audit_store or get_audit_store()
        self._lead_store = lead_store or get_lead_store()

    # ---- send-point wrapping (called from telegram_commands.py / ai_reply.py) ----

    async def prepare_answer(
        self, *, bot_id: str, session: Any, channel: str, telegram_tester_id: Any, chat_id: Any,
        source: str, outcome: str, reply_text: str, client_message_id: str | None = None,
        **context_fields: Any,
    ) -> PreparedAnswer:
        """Creates the `answer_context` row (mints `feedback_token`) BEFORE the reply
        is sent — EVERY automatic answer gets exactly one row here, regardless of
        whether buttons end up shown (buttons are a DISPLAY gate, not a write gate)."""
        conv = session.conversation
        lead = session.lead
        ctx = await self._ctx_store.create(
            conversation_id=conv.id, lead_id=(lead.id if lead else None), session_id=conv.session_id,
            bot_id=bot_id, channel=channel, telegram_tester_id=str(telegram_tester_id), chat_id=str(chat_id),
            client_message_id=client_message_id, source=source, outcome=outcome, reply_text=reply_text,
            **context_fields,
        )
        markup = None
        if settings.telegram_feedback_enabled and allowlist.is_allowed(telegram_tester_id, chat_id):
            markup = build_feedback_keyboard(ctx.feedback_token)
        return PreparedAnswer(context_id=ctx.id, feedback_token=ctx.feedback_token, reply_markup=markup)

    async def finalize_answer(
        self, context_id: int, *, bot_message_id: str | None, provider_bot_message_id: str | None = None,
    ) -> None:
        """Best-effort — a failure here must never break the reply already sent (same
        convention as every other post-send log write in this codebase)."""
        try:
            await self._ctx_store.attach_sent_message(
                context_id, bot_message_id=bot_message_id, provider_bot_message_id=provider_bot_message_id,
            )
        except Exception:  # noqa: BLE001
            log.warning("answer_context finalize (attach_sent_message) failed", exc_info=True)

    # ---- callback handling (§7 authorization, §8 idempotency, §14 fast ack) ----

    async def _reject(self, adapter: Any, callback_query_id: str, *, reason: str, bot_id: str, tester_id: Any) -> None:
        """§7: on ANY authorization violation — no Feedback write, no LLM call, a
        neutral callback answer, and a technical audit event with NO extra data (only
        the short internal reason code, never raw callback_data/text/PII)."""
        try:
            await self._audit_store.record(
                event_type="feedback_callback_rejected", source="telegram_test",
                actor=f"telegram_tester:{tester_id}", reason=reason,
            )
        except Exception:  # noqa: BLE001
            log.warning("feedback callback-rejected audit write failed", exc_info=True)
        await _safe_ack(adapter, callback_query_id, _NEUTRAL_REJECT_TEXT)

    async def handle_callback(
        self, *, bot_id: str, adapter: Any, callback_query_id: str, tester_id: Any, chat_id: Any, data: str,
    ) -> None:
        """Thin entry point for `app/main.py`'s webhook handler. NEVER calls
        FAQ/OpenRouter/route_message — only reads `answer_context`/`feedback` and
        writes `feedback`/audit. Order: basic verify -> record (fast, local) ->
        answerCallbackQuery (§14)."""
        parsed = _parse_callback_data(data)
        if parsed is None:
            await self._reject(adapter, callback_query_id, reason="malformed_callback_data", bot_id=bot_id, tester_id=tester_id)
            return
        token, code = parsed

        if not allowlist.is_allowed(tester_id, chat_id):
            await self._reject(adapter, callback_query_id, reason="not_allowlisted", bot_id=bot_id, tester_id=tester_id)
            return

        ctx = await self._ctx_store.get_by_token(token)
        if ctx is None:
            await self._reject(adapter, callback_query_id, reason="unknown_token", bot_id=bot_id, tester_id=tester_id)
            return
        if ctx.bot_id != bot_id:
            await self._reject(adapter, callback_query_id, reason="bot_mismatch", bot_id=bot_id, tester_id=tester_id)
            return
        if ctx.telegram_tester_id != str(tester_id) or ctx.chat_id != str(chat_id):
            await self._reject(adapter, callback_query_id, reason="tester_mismatch", bot_id=bot_id, tester_id=tester_id)
            return
        if ctx.source not in FEEDBACK_ELIGIBLE_SOURCES:
            await self._reject(adapter, callback_query_id, reason="not_automatic_answer", bot_id=bot_id, tester_id=tester_id)
            return
        if not ctx.bot_message_id:
            await self._reject(adapter, callback_query_id, reason="answer_not_sent", bot_id=bot_id, tester_id=tester_id)
            return
        conv = await self._lead_store.get_conversation(ctx.conversation_id) if ctx.conversation_id else None
        if conv is None or conv.bot_id != bot_id or conv.external_user_id != str(tester_id):
            await self._reject(adapter, callback_query_id, reason="session_not_owned", bot_id=bot_id, tester_id=tester_id)
            return

        if code == COMMENT_CODE:
            set_pending_comment(bot_id, tester_id, ctx.session_id, ctx.id)
            # A show_alert popup vanishes and leaves comment mode invisible (Fable UX
            # review #1) — send a PERSISTENT chat message so the tester keeps seeing
            # they are in comment mode and how to exit. §16: this message goes straight
            # through the adapter and is NEVER logged to the panel (no `add_message`) —
            # exactly how command replies are sent — so it can never enter the LLM
            # history (`_build_history_messages` reads only panel messages). The plain
            # (non-alert) ack just clears the button spinner.
            try:
                await adapter.send(str(chat_id), COMMENT_MODE_MESSAGE)
            except Exception:  # noqa: BLE001 — a send hiccup must not break the ack/pending
                log.warning("comment-mode message send failed", exc_info=True)
            await _safe_ack(adapter, callback_query_id, _COMMENT_ACK_TEXT)
            return

        rating = CODE_TO_RATING.get(code)
        if rating is None:
            await self._reject(adapter, callback_query_id, reason="unknown_code", bot_id=bot_id, tester_id=tester_id)
            return

        view, action = await self._fb_store.create_or_update_rating(
            answer_context_id=ctx.id, telegram_tester_id=str(tester_id), rating=rating,
            conversation_id=ctx.conversation_id, lead_id=ctx.lead_id, session_id=ctx.session_id, bot_id=bot_id,
        )
        if action == "created":
            await self._safe_audit(
                event_type="feedback_recorded", lead_id=ctx.lead_id, conversation_id=ctx.conversation_id,
                actor=f"telegram_tester:{tester_id}", source="telegram_test",
                metadata={"feedback_id": view.id, "rating": rating},
            )
            await _safe_ack(adapter, callback_query_id, _first_rating_text(rating))
        elif action == "updated":
            await self._safe_audit(
                event_type="feedback_rating_changed", lead_id=ctx.lead_id, conversation_id=ctx.conversation_id,
                actor=f"telegram_tester:{tester_id}", source="telegram_test",
                metadata={"feedback_id": view.id, "rating": rating},
            )
            await _safe_ack(adapter, callback_query_id, _updated_rating_text(rating))
        else:  # "noop" — same rating pressed twice, still ack so the spinner clears
            await _safe_ack(adapter, callback_query_id, _first_rating_text(rating))

    async def _safe_audit(self, **fields: Any) -> None:
        try:
            await self._audit_store.record(**fields)
        except Exception:  # noqa: BLE001
            log.warning("feedback audit write failed", exc_info=True)

    # ---- pending-comment consumption (§9) — called from route_message BEFORE FAQ/LLM ----

    async def try_consume_pending_comment(
        self, *, bot_id: str, tester_id: Any, session_id: str, text: str,
    ) -> tuple[bool, str | None]:
        """Returns `(consumed, reply_text)`. `consumed=True` means the caller MUST NOT
        forward `text` to FAQ/LLM/qualification/panel-client-log and MUST send
        `reply_text` back instead (§16 — never logged as a client message either,
        which `route_message` guarantees simply by returning early here without
        calling `_log_to_legacy_panel`)."""
        answer_context_id = get_pending_comment_target(bot_id, tester_id, session_id)
        if answer_context_id is None:
            return False, None
        clear_pending_comment(bot_id, tester_id, session_id)
        ctx = await self._ctx_store.get(answer_context_id)
        if ctx is None:
            return True, COMMENT_TARGET_MISSING_TEXT
        await self._fb_store.create_or_update_comment(
            answer_context_id=ctx.id, telegram_tester_id=str(tester_id), comment=text.strip(),
            conversation_id=ctx.conversation_id, lead_id=ctx.lead_id, session_id=ctx.session_id, bot_id=bot_id,
        )
        await self._safe_audit(
            event_type="feedback_comment_saved", lead_id=ctx.lead_id, conversation_id=ctx.conversation_id,
            actor=f"telegram_tester:{tester_id}", source="telegram_test", metadata={"via": "pending_button"},
        )
        return True, COMMENT_SAVED_TEXT

    def clear_pending_for_cancel(self, bot_id: str, tester_id: Any, session_id: str) -> str:
        """`/cancel` (§9) — never reaches FAQ/LLM either way, this is a pure state clear."""
        had_pending = clear_pending_comment(bot_id, tester_id, session_id)
        return CANCEL_CLEARED_TEXT if had_pending else CANCEL_NOTHING_TEXT

    # ---- /feedback <text> command (§10) ----

    async def attach_feedback_command_comment(
        self, *, bot_id: str, tester_id: Any, session_id: str, comment: str,
    ) -> str:
        """Attaches `comment` to the LAST automatic answer_context of the current
        session. Never touches FAQ/LLM/qualification/lead_status."""
        ctx = await self._ctx_store.get_latest_automatic_for_session(bot_id, session_id)
        if ctx is None:
            return NO_AUTOMATIC_ANSWER_TEXT
        await self._fb_store.create_or_update_comment(
            answer_context_id=ctx.id, telegram_tester_id=str(tester_id), comment=comment,
            conversation_id=ctx.conversation_id, lead_id=ctx.lead_id, session_id=ctx.session_id, bot_id=bot_id,
        )
        await self._safe_audit(
            event_type="feedback_comment_saved", lead_id=ctx.lead_id, conversation_id=ctx.conversation_id,
            actor=f"telegram_tester:{tester_id}", source="telegram_test", metadata={"via": "command"},
        )
        return COMMENT_SAVED_TEXT

    # ---- review-backend methods (§17/§18) — Increment 8 builds the admin UI on top ----

    async def list_feedback(
        self, *, rating: str | None = None, review_status: str | None = None, bot_id: str | None = None,
        tester_id: Any = None, source: str | None = None, intent: str | None = None,
        applied_status: str | None = None, language: str | None = None,
        date_from: datetime | None = None, date_to: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Returns `[{"feedback": FeedbackView, "answer_context": AnswerContextView|None}, ...]`.
        Composed in Python (pilot scale, no need for a cross-table SQL join yet) —
        feedback-native filters (`rating`/`review_status`/`bot_id`/`tester_id`/date
        range) apply to the `feedback` row, answer-context-native filters
        (`source`/`intent`/`applied_status`/`language`) apply to its linked
        `answer_context` row."""
        rows = await self._fb_store.list_all()
        out: list[dict[str, Any]] = []
        for fb in rows:
            if rating is not None and fb.rating != rating:
                continue
            if review_status is not None and fb.review_status != review_status:
                continue
            if bot_id is not None and fb.bot_id != bot_id:
                continue
            if tester_id is not None and fb.telegram_tester_id != str(tester_id):
                continue
            if date_from is not None and (fb.created_at is None or fb.created_at < date_from):
                continue
            if date_to is not None and (fb.created_at is None or fb.created_at > date_to):
                continue
            ctx = await self._ctx_store.get(fb.answer_context_id)
            if source is not None and (ctx is None or ctx.source != source):
                continue
            if intent is not None and (ctx is None or ctx.intent != intent):
                continue
            if applied_status is not None and (ctx is None or ctx.applied_status != applied_status):
                continue
            if language is not None and (ctx is None or ctx.language != language):
                continue
            out.append({"feedback": fb, "answer_context": ctx})
        return out

    async def get_feedback(self, feedback_id: int) -> dict[str, Any] | None:
        fb = await self._fb_store.get(feedback_id)
        if fb is None:
            return None
        ctx = await self._ctx_store.get(fb.answer_context_id)
        return {"feedback": fb, "answer_context": ctx}

    async def update_feedback_review(
        self, feedback_id: int, *, review_status: str, reviewed_by: str | None = None,
        resolution_note: str | None = None,
    ) -> FeedbackView | None:
        updated = await self._fb_store.update_review(
            feedback_id, review_status=review_status, reviewed_by=reviewed_by, resolution_note=resolution_note,
        )
        if updated is not None:
            await self._safe_audit(
                event_type="feedback_review_updated", lead_id=updated.lead_id, conversation_id=updated.conversation_id,
                actor=reviewed_by, source="admin",
                metadata={"feedback_id": feedback_id, "review_status": review_status},
            )
        return updated

    async def set_expected_correction(
        self, feedback_id: int, *, expected_answer: str | None = None, expected_intent: str | None = None,
        expected_status: str | None = None, expected_handoff: bool | None = None,
    ) -> FeedbackView | None:
        return await self._fb_store.set_expected_correction(
            feedback_id, expected_answer=expected_answer, expected_intent=expected_intent,
            expected_status=expected_status, expected_handoff=expected_handoff,
        )

    async def get_answer_context(self, answer_context_id: int) -> AnswerContextView | None:
        return await self._ctx_store.get(answer_context_id)

    async def get_feedback_statistics(self, *, bot_id: str | None = None) -> dict[str, Any]:
        """Pilot QUALITY metrics (NOT business conversion — that stays LeadStatusService/
        the admin funnel). §18: total rated, per-rating counts, unreviewed count,
        faq/llm/fallback share (among RATED feedback), count of feedback-eligible
        answers with NO feedback at all."""
        all_fb = await self._fb_store.list_all()
        if bot_id is not None:
            all_fb = [f for f in all_fb if f.bot_id == bot_id]
        rated = [f for f in all_fb if f.rating is not None]

        per_rating = dict.fromkeys(RATINGS, 0)
        for f in rated:
            if f.rating in per_rating:
                per_rating[f.rating] += 1

        unreviewed_count = sum(1 for f in all_fb if f.review_status == "unreviewed")

        source_share = {"faq": 0, "llm": 0, "fallback": 0}
        for f in rated:
            ctx = await self._ctx_store.get(f.answer_context_id)
            if ctx is None:
                continue
            if ctx.source == "faq":
                source_share["faq"] += 1
            elif ctx.source == "llm":
                source_share["llm"] += 1
            else:
                source_share["fallback"] += 1

        eligible = await self._ctx_store.list_eligible(bot_id=bot_id)
        rated_context_ids = {f.answer_context_id for f in all_fb}
        no_feedback_count = sum(1 for ctx in eligible if ctx.id not in rated_context_ids)

        return {
            "total_rated": len(rated),
            "per_rating": per_rating,
            "unreviewed_count": unreviewed_count,
            "source_share": source_share,
            "no_feedback_count": no_feedback_count,
            "total_eligible_answers": len(eligible),
        }


async def _safe_ack(adapter: Any, callback_query_id: str, text: str, *, show_alert: bool = False) -> None:
    """§14: answer the callback quickly so the Telegram spinner doesn't hang; never
    raises — a Telegram API hiccup on the ack must not break the webhook response."""
    try:
        await adapter.answer_callback(callback_query_id, text, show_alert=show_alert)
    except Exception:  # noqa: BLE001
        log.warning("answerCallbackQuery failed", exc_info=True)

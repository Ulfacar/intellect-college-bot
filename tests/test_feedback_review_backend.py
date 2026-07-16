"""Increment 7: review-backend methods on FeedbackService (brief scenarios 45-50,
§17/§18) — list_feedback filters, get_feedback, update_feedback_review,
set_expected_correction, get_answer_context, get_feedback_statistics. Backend methods
only — the admin UI itself is Increment 8 (out of scope here)."""
from __future__ import annotations

import asyncio

import pytest

from app.core.feedback_service import FeedbackService
from app.integrations.panel.answer_context_store import get_answer_context_store, reset as reset_ctx
from app.integrations.panel.audit_store import get_audit_store
from app.integrations.panel.feedback_store import get_feedback_store, reset as reset_fb


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate():
    reset_ctx()
    reset_fb()
    yield
    reset_ctx()
    reset_fb()


async def _make_rated(bot_id, tester_id, *, source, outcome, rating, review_status="unreviewed",
                       intent=None, applied_status=None, language=None):
    ctx_store = get_answer_context_store()
    ctx = await ctx_store.create(
        conversation_id=1, lead_id=1, session_id=f"s-{tester_id}", bot_id=bot_id, channel="telegram",
        telegram_tester_id=tester_id, chat_id=tester_id, source=source, outcome=outcome, reply_text="x",
        intent=intent, applied_status=applied_status, language=language,
    )
    await ctx_store.attach_sent_message(ctx.id, bot_message_id="1")
    fb_store = get_feedback_store()
    view, _action = await fb_store.create_or_update_rating(
        answer_context_id=ctx.id, telegram_tester_id=tester_id, rating=rating,
        conversation_id=1, lead_id=1, session_id=f"s-{tester_id}", bot_id=bot_id,
    )
    if review_status != "unreviewed":
        view = await fb_store.update_review(view.id, review_status=review_status)
    return ctx, view


# 45. list_feedback filters by rating.
def test_list_feedback_filters_by_rating():
    async def scenario():
        await _make_rated("b1", "t1", source="faq", outcome="faq_answered", rating="correct")
        await _make_rated("b1", "t2", source="llm", outcome="llm_answered", rating="incorrect")
        svc = FeedbackService()
        only_incorrect = await svc.list_feedback(rating="incorrect")
        assert len(only_incorrect) == 1
        assert only_incorrect[0]["feedback"].rating == "incorrect"
    _run(scenario())


# 46. list_feedback filters by review_status/bot_id/tester_id.
def test_list_feedback_filters_by_review_status_bot_and_tester():
    async def scenario():
        await _make_rated("bA", "tA", source="faq", outcome="faq_answered", rating="correct", review_status="fixed")
        await _make_rated("bB", "tB", source="faq", outcome="faq_answered", rating="correct")
        svc = FeedbackService()
        assert len(await svc.list_feedback(review_status="fixed")) == 1
        assert len(await svc.list_feedback(bot_id="bA")) == 1
        assert len(await svc.list_feedback(tester_id="tB")) == 1
        assert len(await svc.list_feedback(bot_id="bA", tester_id="tB")) == 0
    _run(scenario())


# 47. list_feedback filters by source/intent/applied_status/language (joined from
# the linked answer_context).
def test_list_feedback_filters_by_answer_context_fields():
    async def scenario():
        await _make_rated("bC", "tC1", source="faq", outcome="faq_answered", rating="correct",
                           intent="cost_info", applied_status="info_sent", language="ru")
        await _make_rated("bC", "tC2", source="llm", outcome="llm_answered", rating="correct",
                           intent="directions", applied_status="in_progress", language="ky")
        svc = FeedbackService()
        assert len(await svc.list_feedback(source="faq")) == 1
        assert len(await svc.list_feedback(intent="directions")) == 1
        assert len(await svc.list_feedback(applied_status="info_sent")) == 1
        assert len(await svc.list_feedback(language="ky")) == 1
        assert len(await svc.list_feedback(source="faq", language="ky")) == 0
    _run(scenario())


# 48. get_feedback returns the combined view; update_feedback_review updates the row
# + writes an audit event.
def test_get_feedback_and_update_review():
    async def scenario():
        ctx, fb = await _make_rated("bD", "tD", source="faq", outcome="faq_answered", rating="inaccurate")
        svc = FeedbackService()
        got = await svc.get_feedback(fb.id)
        assert got is not None
        assert got["feedback"].id == fb.id
        assert got["answer_context"].id == ctx.id

        updated = await svc.update_feedback_review(
            fb.id, review_status="fixed", reviewed_by="aidana", resolution_note="Поправили FAQ",
        )
        assert updated.review_status == "fixed"
        assert updated.reviewed_by == "aidana"
        assert updated.resolution_note == "Поправили FAQ"
        assert updated.reviewed_at is not None

        audit = await get_audit_store().list_for_lead(1)
        review_events = [a for a in audit if a.event_type == "feedback_review_updated"]
        assert len(review_events) >= 1

        assert await svc.update_feedback_review(999999, review_status="fixed") is None
    _run(scenario())


# 49. set_expected_correction sets expected_answer/intent/status/handoff.
def test_set_expected_correction():
    async def scenario():
        _ctx, fb = await _make_rated("bE", "tE", source="faq", outcome="faq_answered", rating="incorrect")
        svc = FeedbackService()
        updated = await svc.set_expected_correction(
            fb.id, expected_answer="Правильный ответ: 6500$/год", expected_intent="cost_info",
            expected_status="info_sent", expected_handoff=False,
        )
        assert updated.expected_answer == "Правильный ответ: 6500$/год"
        assert updated.expected_intent == "cost_info"
        assert updated.expected_status == "info_sent"
        assert updated.expected_handoff is False
        # rating/review_status are untouched by this call (separate axes).
        assert updated.rating == "incorrect"
        assert updated.review_status == "unreviewed"
    _run(scenario())


# 50. get_feedback_statistics: total rated, per-rating counts, unreviewed count,
# faq/llm/fallback share, count of feedback-eligible answers with NO feedback.
def test_get_feedback_statistics():
    async def scenario():
        bot_id = "bF"
        await _make_rated(bot_id, "tF1", source="faq", outcome="faq_answered", rating="correct")
        await _make_rated(bot_id, "tF2", source="llm", outcome="llm_answered", rating="incorrect")
        await _make_rated(bot_id, "tF3", source="safe_fallback", outcome="validator_blocked", rating="correct",
                           review_status="fixed")
        # an eligible answer that got NO feedback at all.
        ctx_store = get_answer_context_store()
        unrated = await ctx_store.create(
            conversation_id=1, lead_id=1, session_id="s-unrated", bot_id=bot_id, channel="telegram",
            telegram_tester_id="tF4", chat_id="tF4", source="faq", outcome="faq_answered", reply_text="x",
        )
        await ctx_store.attach_sent_message(unrated.id, bot_message_id="1")

        svc = FeedbackService()
        stats = await svc.get_feedback_statistics(bot_id=bot_id)
        assert stats["total_rated"] == 3
        assert stats["per_rating"]["correct"] == 2
        assert stats["per_rating"]["incorrect"] == 1
        assert stats["unreviewed_count"] == 2  # 3 rated - 1 marked "fixed"
        assert stats["source_share"] == {"faq": 1, "llm": 1, "fallback": 1}
        assert stats["no_feedback_count"] == 1
        assert stats["total_eligible_answers"] == 4
    _run(scenario())

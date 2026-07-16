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
                       intent=None, applied_status=None, language=None, axis="quality"):
    ctx_store = get_answer_context_store()
    ctx = await ctx_store.create(
        conversation_id=1, lead_id=1, session_id=f"s-{tester_id}", bot_id=bot_id, channel="telegram",
        telegram_tester_id=tester_id, chat_id=tester_id, source=source, outcome=outcome, reply_text="x",
        intent=intent, applied_status=applied_status, language=language,
    )
    await ctx_store.attach_sent_message(ctx.id, bot_message_id="1")
    fb_store = get_feedback_store()
    view, _action = await fb_store.set_axis_rating(
        answer_context_id=ctx.id, telegram_tester_id=tester_id, axis=axis, value=rating,
        conversation_id=1, lead_id=1, session_id=f"s-{tester_id}", bot_id=bot_id,
    )
    if review_status != "unreviewed":
        view = await fb_store.update_review(view.id, review_status=review_status)
    return ctx, view


# 45. list_feedback filters by quality_rating (Increment 7.1 — supersedes the old
# single-axis `rating` filter, which is unaffected since it's a separate parameter
# still pointed at the LEGACY `rating` column, see test 45c below).
def test_list_feedback_filters_by_quality_rating():
    async def scenario():
        await _make_rated("b1", "t1", source="faq", outcome="faq_answered", rating="correct", axis="quality")
        await _make_rated("b1", "t2", source="llm", outcome="llm_answered", rating="incorrect", axis="quality")
        svc = FeedbackService()
        only_incorrect = await svc.list_feedback(quality_rating="incorrect")
        assert len(only_incorrect) == 1
        assert only_incorrect[0]["feedback"].quality_rating == "incorrect"
    _run(scenario())


# 45b (brief scenario 11: strategy filter). list_feedback filters by strategy_rating,
# independently of quality_rating — one axis's filter never matches on the other.
def test_list_feedback_filters_by_strategy_rating():
    async def scenario():
        await _make_rated("b2", "t3", source="faq", outcome="faq_answered", rating="should_push", axis="strategy")
        await _make_rated("b2", "t4", source="llm", outcome="llm_answered", rating="should_handoff", axis="strategy")
        svc = FeedbackService()
        only_handoff = await svc.list_feedback(strategy_rating="should_handoff")
        assert len(only_handoff) == 1
        assert only_handoff[0]["feedback"].strategy_rating == "should_handoff"
        assert only_handoff[0]["feedback"].quality_rating is None
        # combining both axis filters is independent — no row has BOTH set here.
        assert await svc.list_feedback(quality_rating="correct", strategy_rating="should_handoff") == []
    _run(scenario())


# 45c. list_feedback's `rating` parameter still filters the LEGACY column (kept
# working per the brief) — a row carrying a legacy `rating` value (e.g. a pre-Increment
# 7.1 row, or one backfilled by migration 0006) is still found by it.
def test_list_feedback_legacy_rating_filter_still_works():
    async def scenario():
        ctx_store = get_answer_context_store()
        ctx = await ctx_store.create(
            conversation_id=1, lead_id=1, session_id="s-legacy", bot_id="b3", channel="telegram",
            telegram_tester_id="t5", chat_id="t5", source="faq", outcome="faq_answered", reply_text="x",
        )
        await ctx_store.attach_sent_message(ctx.id, bot_message_id="1")
        fb_store = get_feedback_store()
        fb_store._seq += 1
        from app.integrations.panel.feedback_store import FeedbackView
        row = FeedbackView(id=fb_store._seq, answer_context_id=ctx.id, telegram_tester_id="t5",
                            bot_id="b3", session_id="s-legacy", rating="correct")
        fb_store._rows[row.id] = row

        svc = FeedbackService()
        found = await svc.list_feedback(rating="correct")
        assert len(found) == 1
        assert found[0]["feedback"].rating == "correct"
        assert found[0]["feedback"].quality_rating is None  # never backfilled by app code
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
        # rating axes/review_status are untouched by this call (separate axes).
        assert updated.quality_rating == "incorrect"
        assert updated.strategy_rating is None
        assert updated.review_status == "unreviewed"
    _run(scenario())


# 50 (brief scenario 12: stats split by two axes). get_feedback_statistics: quality
# and strategy are computed as two SEPARATE, non-merged axes, each with per-value
# counts and its own "answers missing THIS axis's rating" count; unreviewed_count/
# source_share/no_feedback_count stay axis-agnostic (Increment 7.1).
def test_get_feedback_statistics_split_by_axis():
    async def scenario():
        bot_id = "bF"
        await _make_rated(bot_id, "tF1", source="faq", outcome="faq_answered", rating="correct", axis="quality")
        await _make_rated(bot_id, "tF2", source="llm", outcome="llm_answered", rating="incorrect", axis="quality")
        await _make_rated(bot_id, "tF3", source="safe_fallback", outcome="validator_blocked",
                           rating="correct", axis="quality", review_status="fixed")
        # a strategy-only rating on a NEW answer — never touches quality.
        await _make_rated(bot_id, "tF5", source="faq", outcome="faq_answered",
                           rating="should_handoff", axis="strategy")
        # an eligible answer that got NO feedback at all.
        ctx_store = get_answer_context_store()
        unrated = await ctx_store.create(
            conversation_id=1, lead_id=1, session_id="s-unrated", bot_id=bot_id, channel="telegram",
            telegram_tester_id="tF4", chat_id="tF4", source="faq", outcome="faq_answered", reply_text="x",
        )
        await ctx_store.attach_sent_message(unrated.id, bot_message_id="1")

        svc = FeedbackService()
        stats = await svc.get_feedback_statistics(bot_id=bot_id)

        assert stats["quality"]["total_rated"] == 3
        assert stats["quality"]["per_rating"]["correct"] == 2
        assert stats["quality"]["per_rating"]["incorrect"] == 1
        assert stats["quality"]["per_rating"]["inaccurate"] == 0
        # 5 eligible total (4 rated ctx's + 1 unrated) - 3 with a quality_rating = 2
        assert stats["quality"]["answers_without_quality_rating"] == 2

        assert stats["strategy"]["total_rated"] == 1
        assert stats["strategy"]["per_rating"]["should_handoff"] == 1
        assert stats["strategy"]["per_rating"]["appropriate"] == 0
        assert stats["strategy"]["answers_without_strategy_rating"] == 4

        assert stats["unreviewed_count"] == 3  # 4 feedback rows - 1 marked "fixed"
        assert stats["source_share"] == {"faq": 2, "llm": 1, "fallback": 1}
        assert stats["no_feedback_count"] == 1
        assert stats["total_eligible_answers"] == 5

        # never merged into one overall percent/count.
        assert "per_rating" not in stats
        assert "total_rated" not in stats
    _run(scenario())

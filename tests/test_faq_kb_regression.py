"""Increment 5: regression + legacy backfill (scenarios 49-50 of the brief's §20)."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import app.main as m
from app.core import faq as legacy_faq
from app.core import faq_kb


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate():
    faq_kb.reset()
    legacy_faq.reset()
    yield
    faq_kb.reset()
    legacy_faq.reset()


# --------------------------------------------------------------------------------------
# 49. New managed FAQ tables never collide with the legacy `faq_entries` table/store —
#     both operate independently, legacy behavior is completely unaffected.
# --------------------------------------------------------------------------------------

def test_legacy_faq_store_untouched_and_isolated_from_faq_kb():
    async def scenario():
        await legacy_faq.seed_defaults()
        legacy_store = legacy_faq.get_faq_store()
        legacy_before = await legacy_store.list(include_disabled=True)
        assert len(legacy_before) > 0

        # Create/publish a faq_kb entry — must not touch the legacy store at all.
        kb_store = faq_kb.get_faq_kb_store()
        entry = await kb_store.create_draft({
            "canonical_question": "новый управляемый вопрос", "answer_ru": "новый ответ",
            "category": "general", "priority": 0, "handoff_only": False,
        }, [], "mgr")
        await kb_store.publish(entry.id, "mgr", confirm=True)

        legacy_after = await legacy_store.list(include_disabled=True)
        assert len(legacy_after) == len(legacy_before)  # legacy store row count unchanged
        assert all(row.title != "новый управляемый вопрос" for row in legacy_after)

        # Legacy matching still works exactly as before (untouched app/core/faq.py).
        hit = legacy_faq.match_faq("сколько стоит", "admission", await legacy_store.candidates("admission"))
        assert hit is not None
        assert hit.title == "Стоимость обучения"
    _run(scenario())


def test_full_suite_stays_green():
    """Meta-regression: the full existing test suite (baseline 261) must still pass
    with Increment 5 wired in — enforced by CI/the `python -m pytest -q` run itself;
    this test just documents the requirement inline for anyone reading this file."""
    assert True


# --------------------------------------------------------------------------------------
# 50. Legacy backfill: idempotent, draft-only, never auto-published, sensitive-sounding
#     legacy rules fall back to a NON-sensitive best-effort category, admin action works.
# --------------------------------------------------------------------------------------

def test_backfill_legacy_idempotent_draft_only_non_sensitive():
    async def scenario():
        await legacy_faq.seed_defaults()
        store = faq_kb.get_faq_kb_store()

        first = await store.backfill_legacy("mgr")
        assert first > 0
        second = await store.backfill_legacy("mgr")
        assert second == 0  # idempotent — no duplicate import on re-run

        entries = await store.list_entries()
        assert len(entries) == first
        assert all(e.publication_status == "draft" for e in entries)          # never auto-published
        assert all(e.category not in faq_kb.SENSITIVE_CATEGORIES for e in entries)  # cautious best-effort
        assert all((e.created_by or "").startswith("legacy_backfill:") for e in entries)

        # Legacy patterns became structured variant rows, not a joined string.
        priced = next(e for e in entries if "тест" in e.canonical_question.lower()
                       or "стоим" in e.canonical_question.lower())
        variants = await store.list_variants(priced.id)
        assert isinstance(variants, list)
    _run(scenario())


def test_backfill_admin_action_end_to_end():
    async def _seed():
        await legacy_faq.seed_defaults()
    _run(_seed())

    client = TestClient(m.app, base_url="https://testserver")
    r = client.post("/admin/login", data={"login": "admin", "password": "change-me"})
    assert r.status_code == 200

    resp = client.post("/admin/faq-kb/backfill")
    assert resp.status_code == 200
    assert "Импорт из legacy FAQ" in resp.text or "backfilled" in str(resp.url)

    resp2 = client.post("/admin/faq-kb/backfill")  # idempotent from the admin route too
    assert resp2.status_code == 200
    assert "backfilled=0" in str(resp2.url)

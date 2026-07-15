"""Increment 5: managed FAQ / knowledge base — publication lifecycle (scenarios 1-10 of
the brief's §20). Contract tests are parametrized over MemoryFaqKbStore and
PostgresFaqKbStore on SQLite-in-memory (StaticPool, no real network Postgres — same
convention as tests/test_leadstore.py / tests/test_crm_postgres.py) so both backends are
proven to behave identically, no skips.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.faq_kb import MemoryFaqKbStore, PostgresFaqKbStore
from app.integrations.crm.db import init_models

BACKENDS = ["memory", "postgres"]


async def _pg_store() -> PostgresFaqKbStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    await init_models(engine)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    return PostgresFaqKbStore(sessionmaker=sm)


async def _store(kind: str):
    return MemoryFaqKbStore() if kind == "memory" else await _pg_store()


def _run(coro):
    return asyncio.run(coro)


def _draft_data(**overrides):
    data = {
        "canonical_question": "Сколько стоит обучение?",
        "answer_ru": "Стоимость 6500 долларов в год.",
        "answer_ky": None,
        "category": "tuition",
        "priority": 10,
        "handoff_only": False,
    }
    data.update(overrides)
    return data


# --------------------------------------------------------------------------------------
# 1. create_draft -> publication_status=draft, never served.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_create_draft_never_served(backend):
    async def scenario():
        store = await _store(backend)
        entry = await store.create_draft(_draft_data(), [{"text": "цена обучения"}], "mgr")
        assert entry.publication_status == "draft"
        candidates = await store.list_published_candidates()
        assert candidates == []
    _run(scenario())


# --------------------------------------------------------------------------------------
# 2. Draft stays invisible even when enabled=True (draft never answers, period).
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_enabled_draft_still_not_served(backend):
    async def scenario():
        store = await _store(backend)
        entry = await store.create_draft(_draft_data(), [], "mgr")
        assert entry.enabled is True  # enabled by default
        assert await store.list_published_candidates() == []
    _run(scenario())


# --------------------------------------------------------------------------------------
# 3. Publish -> published, version(action=published) created, now served.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_publish_creates_version_and_serves(backend):
    async def scenario():
        store = await _store(backend)
        entry = await store.create_draft(_draft_data(), [], "mgr")
        result = await store.publish(entry.id, "mgr", confirm=True)
        assert result.ok
        assert result.entry.publication_status == "published"
        assert result.entry.published_by == "mgr"
        versions = await store.list_versions(entry.id)
        published = [v for v in versions if v.action == "published"]
        assert len(published) == 1

        candidates = await store.list_published_candidates()
        assert len(candidates) == 1
        assert candidates[0].faq_entry_id == entry.id
    _run(scenario())


# --------------------------------------------------------------------------------------
# 4. Editing a published entry does NOT change what's served until re-Publish.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_editing_published_entry_does_not_change_production(backend):
    async def scenario():
        store = await _store(backend)
        entry = await store.create_draft(_draft_data(answer_ru="СТАРЫЙ ответ"), [], "mgr")
        await store.publish(entry.id, "mgr", confirm=True)

        await store.update_draft(entry.id, _draft_data(answer_ru="НОВЫЙ ответ"), [], "mgr")

        candidates = await store.list_published_candidates()
        assert candidates[0].answer_ru == "СТАРЫЙ ответ"  # still serving the old snapshot

        live = await store.get_entry(entry.id)
        assert live.answer_ru == "НОВЫЙ ответ"  # live/editable content already updated
    _run(scenario())


# --------------------------------------------------------------------------------------
# 5. Re-publish after edit updates the served answer.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_republish_updates_served_answer(backend):
    async def scenario():
        store = await _store(backend)
        entry = await store.create_draft(_draft_data(answer_ru="СТАРЫЙ ответ"), [], "mgr")
        await store.publish(entry.id, "mgr", confirm=True)
        await store.update_draft(entry.id, _draft_data(answer_ru="НОВЫЙ ответ"), [], "mgr")

        result = await store.publish(entry.id, "mgr", confirm=True)
        assert result.ok

        candidates = await store.list_published_candidates()
        assert candidates[0].answer_ru == "НОВЫЙ ответ"
    _run(scenario())


# --------------------------------------------------------------------------------------
# 6. Disable takes effect IMMEDIATELY (no publish cycle) and requires confirm when published.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_disable_published_entry_immediate_and_needs_confirm(backend):
    async def scenario():
        store = await _store(backend)
        entry = await store.create_draft(_draft_data(category="general"), [], "mgr")
        await store.publish(entry.id, "mgr", confirm=True)
        assert len(await store.list_published_candidates()) == 1

        result = await store.disable(entry.id, "mgr", confirm=False)
        assert not result.ok
        assert result.error == "confirmation_required"
        assert len(await store.list_published_candidates()) == 1  # unchanged

        result2 = await store.disable(entry.id, "mgr", confirm=True)
        assert result2.ok
        assert result2.entry.enabled is False
        assert await store.list_published_candidates() == []
    _run(scenario())


# --------------------------------------------------------------------------------------
# 7. Disable a DRAFT (never published) needs no confirmation.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_disable_draft_needs_no_confirm(backend):
    async def scenario():
        store = await _store(backend)
        entry = await store.create_draft(_draft_data(category="general"), [], "mgr")
        result = await store.disable(entry.id, "mgr", confirm=False)
        assert result.ok
        assert result.entry.enabled is False
    _run(scenario())


# --------------------------------------------------------------------------------------
# 8. Enable restores serving (assuming still published + within window).
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_enable_restores_serving(backend):
    async def scenario():
        store = await _store(backend)
        entry = await store.create_draft(_draft_data(category="general"), [], "mgr")
        await store.publish(entry.id, "mgr", confirm=True)
        await store.disable(entry.id, "mgr", confirm=True)
        assert await store.list_published_candidates() == []

        result = await store.enable(entry.id, "mgr")
        assert result.ok
        assert result.entry.enabled is True
        assert len(await store.list_published_candidates()) == 1
    _run(scenario())


# --------------------------------------------------------------------------------------
# 9. Archive = soft delete: requires confirm, never hard-deletes, keeps history.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_archive_is_soft_delete_and_needs_confirm(backend):
    async def scenario():
        store = await _store(backend)
        entry = await store.create_draft(_draft_data(category="general"), [], "mgr")
        await store.publish(entry.id, "mgr", confirm=True)

        blocked = await store.archive(entry.id, "mgr", confirm=False)
        assert not blocked.ok
        assert blocked.error == "confirmation_required"
        assert len(await store.list_published_candidates()) == 1  # unchanged

        result = await store.archive(entry.id, "mgr", confirm=True)
        assert result.ok
        assert result.entry.publication_status == "archived"
        assert result.entry.archived_at is not None
        assert await store.list_published_candidates() == []

        # Soft delete: the row still exists (no hard delete method exists at all).
        still_there = await store.get_entry(entry.id)
        assert still_there is not None
        assert still_there.publication_status == "archived"
        # History preserved.
        assert len(await store.list_versions(entry.id)) >= 2
    _run(scenario())


# --------------------------------------------------------------------------------------
# 10. valid_from/valid_until window gates serving (outside window -> never answers).
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_validity_window_gates_serving(backend):
    async def scenario():
        store = await _store(backend)
        now = datetime.now(timezone.utc)

        future_entry = await store.create_draft(
            _draft_data(category="general", canonical_question="Акция скоро"), [], "mgr",
        )
        await store.update_draft(
            future_entry.id, _draft_data(category="general", canonical_question="Акция скоро",
                                          valid_from=now + timedelta(days=1)), [], "mgr",
        )
        await store.publish(future_entry.id, "mgr", confirm=True)

        past_entry = await store.create_draft(
            _draft_data(category="general", canonical_question="Акция закончилась"), [], "mgr",
        )
        await store.update_draft(
            past_entry.id, _draft_data(category="general", canonical_question="Акция закончилась",
                                        valid_until=now - timedelta(days=1)), [], "mgr",
        )
        await store.publish(past_entry.id, "mgr", confirm=True)

        current_entry = await store.create_draft(
            _draft_data(category="general", canonical_question="Акция сейчас"), [], "mgr",
        )
        await store.update_draft(
            current_entry.id, _draft_data(category="general", canonical_question="Акция сейчас",
                                           valid_from=now - timedelta(days=1),
                                           valid_until=now + timedelta(days=1)), [], "mgr",
        )
        await store.publish(current_entry.id, "mgr", confirm=True)

        served_ids = {c.faq_entry_id for c in await store.list_published_candidates(now=now)}
        assert future_entry.id not in served_ids
        assert past_entry.id not in served_ids
        assert current_entry.id in served_ids
    _run(scenario())

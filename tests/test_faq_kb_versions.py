"""Increment 5: versions/rollback (scenarios 38-43 of the brief's §20). Store-level,
memory backend (the lifecycle file already proves memory/postgres parity for the core
publish/disable/archive transactions; this file focuses on version-history semantics)."""
from __future__ import annotations

import asyncio

import pytest

from app.core import faq_kb


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate():
    faq_kb.reset()
    yield
    faq_kb.reset()


def _data(**overrides):
    base = {
        "canonical_question": "сколько стоит обучение", "answer_ru": "6500 долларов",
        "answer_ky": None, "category": "tuition", "priority": 5, "handoff_only": False,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------------------
# 38. Publish creates version_number=1 first, then monotonically increments.
# --------------------------------------------------------------------------------------

def test_version_numbers_increment_monotonically():
    async def scenario():
        store = faq_kb.get_faq_kb_store()
        entry = await store.create_draft(_data(), [], "mgr")  # v1 = created
        versions = await store.list_versions(entry.id)
        assert [v.version_number for v in versions] == [1]

        await store.publish(entry.id, "mgr", confirm=True)  # v2 = published
        await store.update_draft(entry.id, _data(answer_ru="7000 долларов"), [], "mgr")  # v3 = edited
        await store.publish(entry.id, "mgr", confirm=True)  # v4 = published

        versions = await store.list_versions(entry.id)
        assert [v.version_number for v in versions] == [1, 2, 3, 4]
        assert [v.action for v in versions] == ["created", "published", "edited", "published"]
    _run(scenario())


# --------------------------------------------------------------------------------------
# 39. Editing a published entry never destroys the old published version's snapshot.
# --------------------------------------------------------------------------------------

def test_editing_preserves_old_published_snapshot():
    async def scenario():
        store = faq_kb.get_faq_kb_store()
        entry = await store.create_draft(_data(answer_ru="СТАРЫЙ"), [], "mgr")
        await store.publish(entry.id, "mgr", confirm=True)

        await store.update_draft(entry.id, _data(answer_ru="НОВЫЙ"), [], "mgr")
        await store.publish(entry.id, "mgr", confirm=True)

        versions = await store.list_versions(entry.id)
        published = [v for v in versions if v.action == "published"]
        assert len(published) == 2
        assert published[0].snapshot["answer_ru"] == "СТАРЫЙ"   # untouched
        assert published[1].snapshot["answer_ru"] == "НОВЫЙ"
    _run(scenario())


# --------------------------------------------------------------------------------------
# 40. Rollback without confirm -> confirmation_required, nothing changes.
# --------------------------------------------------------------------------------------

def test_rollback_without_confirm_is_refused():
    async def scenario():
        store = faq_kb.get_faq_kb_store()
        entry = await store.create_draft(_data(answer_ru="v1-ответ"), [], "mgr")
        await store.publish(entry.id, "mgr", confirm=True)
        versions_before = await store.list_versions(entry.id)

        result = await store.rollback(entry.id, 1, "mgr", confirm=False)
        assert not result.ok
        assert result.error == "confirmation_required"

        versions_after = await store.list_versions(entry.id)
        assert len(versions_after) == len(versions_before)  # no new row appended
    _run(scenario())


# --------------------------------------------------------------------------------------
# 41. Rollback with confirm -> NEW version (action=restored), history never deleted.
# --------------------------------------------------------------------------------------

def test_rollback_with_confirm_appends_restored_version():
    async def scenario():
        store = faq_kb.get_faq_kb_store()
        entry = await store.create_draft(_data(answer_ru="v1-ответ"), [], "mgr")
        first_publish = await store.publish(entry.id, "mgr", confirm=True)
        target_version = [v for v in await store.list_versions(entry.id) if v.action == "published"][0].version_number

        await store.update_draft(entry.id, _data(answer_ru="v2-ответ"), [], "mgr")
        await store.publish(entry.id, "mgr", confirm=True)
        count_before = len(await store.list_versions(entry.id))

        result = await store.rollback(entry.id, target_version, "mgr", confirm=True)
        assert result.ok

        versions_after = await store.list_versions(entry.id)
        assert len(versions_after) == count_before + 1  # appended, nothing removed
        assert versions_after[-1].action == "restored"
        assert versions_after[-1].snapshot["answer_ru"] == "v1-ответ"
        assert first_publish.ok  # original publish object untouched/still valid
    _run(scenario())


# --------------------------------------------------------------------------------------
# 42. After rollback, both the served snapshot AND the live editable fields reflect it.
# --------------------------------------------------------------------------------------

def test_rollback_restores_serving_and_live_fields():
    async def scenario():
        store = faq_kb.get_faq_kb_store()
        entry = await store.create_draft(_data(answer_ru="v1-ответ"), [], "mgr")
        await store.publish(entry.id, "mgr", confirm=True)

        await store.update_draft(entry.id, _data(answer_ru="v2-ответ"), [], "mgr")
        await store.publish(entry.id, "mgr", confirm=True)
        assert (await store.list_published_candidates())[0].answer_ru == "v2-ответ"

        await store.rollback(entry.id, 1, "mgr", confirm=True)

        candidates = await store.list_published_candidates()
        assert candidates[0].answer_ru == "v1-ответ"
        live = await store.get_entry(entry.id)
        assert live.answer_ru == "v1-ответ"
        assert live.publication_status == "published"
    _run(scenario())


# --------------------------------------------------------------------------------------
# 43. Snapshot content is limited to entry-content fields — no secrets/tokens/prompts.
# --------------------------------------------------------------------------------------

def test_snapshot_contains_only_content_fields_no_secrets():
    async def scenario():
        store = faq_kb.get_faq_kb_store()
        entry = await store.create_draft(_data(), [{"text": "цена обучения"}], "mgr")
        await store.publish(entry.id, "mgr", confirm=True)
        versions = await store.list_versions(entry.id)
        allowed_keys = {
            "canonical_question", "answer_ru", "answer_ky", "category", "priority",
            "handoff_only", "valid_from", "valid_until", "variants",
        }
        for v in versions:
            assert set(v.snapshot.keys()) <= allowed_keys
            blob = str(v.snapshot).lower()
            for forbidden in ("token", "secret", "api_key", "password", "openrouter"):
                assert forbidden not in blob
    _run(scenario())

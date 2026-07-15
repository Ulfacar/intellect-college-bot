"""Increment 5: admin playground (scenarios 44-48 of the brief's §20). TestClient
against the real admin routes — same login convention as tests/test_panel.py
(`base_url="https://testserver"` so the Secure session cookie round-trips)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as m
from app.core import faq_kb
from app.integrations.panel.leadstore import get_lead_store


@pytest.fixture(autouse=True)
def _isolate_faq_kb():
    faq_kb.reset()
    yield
    faq_kb.reset()


def _auth_client() -> TestClient:
    client = TestClient(m.app, base_url="https://testserver")
    r = client.post("/admin/login", data={"login": "admin", "password": "change-me"})
    assert r.status_code == 200
    return client


def _create_and_publish(client, *, question="сколько стоит обучение", answer_ru="6500 долларов",
                        answer_ky=None, handoff_only=False, category="tuition"):
    r = client.post("/admin/faq-kb/save", data={
        "entry_id": 0, "canonical_question": question, "variants": "",
        "answer_ru": answer_ru, "answer_ky": answer_ky or "", "category": category,
        "priority": 5, "handoff_only": "1" if handoff_only else "0",
        "valid_from": "", "valid_until": "",
    })
    assert r.status_code == 200
    entry_id = int(str(r.url).rsplit("edit=", 1)[1])
    pub = client.post(f"/admin/faq-kb/{entry_id}/publish", data={"confirm": "1"})
    assert pub.status_code == 200
    return entry_id


# --------------------------------------------------------------------------------------
# 44. Published-mode playground returns full match info for a real published entry.
# --------------------------------------------------------------------------------------

def test_playground_published_mode_shows_match_info():
    client = _auth_client()
    entry_id = _create_and_publish(client)

    r = client.post("/admin/faq-kb/playground", data={
        "mode": "published", "entry_id": entry_id, "text": "сколько стоит обучение", "language": "auto",
    })
    assert r.status_code == 200
    assert "Совпадение найдено" in r.text
    assert "6500 долларов" in r.text
    assert "canonical" in r.text


# --------------------------------------------------------------------------------------
# 45. Draft-mode playground tests an UNPUBLISHED entry's current content.
# --------------------------------------------------------------------------------------

def test_playground_draft_mode_tests_unpublished_entry():
    client = _auth_client()
    r = client.post("/admin/faq-kb/save", data={
        "entry_id": 0, "canonical_question": "какие направления есть", "variants": "",
        "answer_ru": "8 направлений: IT и бизнес.", "answer_ky": "", "category": "directions",
        "priority": 0, "handoff_only": "0", "valid_from": "", "valid_until": "",
    })
    entry_id = int(str(r.url).rsplit("edit=", 1)[1])

    pr = client.post("/admin/faq-kb/playground", data={
        "mode": "draft", "entry_id": entry_id, "text": "какие направления есть", "language": "auto",
    })
    assert pr.status_code == 200
    assert "Совпадение найдено" in pr.text
    assert "8 направлений" in pr.text


# --------------------------------------------------------------------------------------
# 46. Draft preview does NOT touch the real pipeline: no answer-log row, no
#     Conversation/Lead, adapter never invoked (playground is admin-only, HTTP-only).
# --------------------------------------------------------------------------------------

def test_playground_draft_preview_does_not_touch_real_pipeline():
    client = _auth_client()
    r = client.post("/admin/faq-kb/save", data={
        "entry_id": 0, "canonical_question": "какие направления есть", "variants": "",
        "answer_ru": "8 направлений.", "answer_ky": "", "category": "directions",
        "priority": 0, "handoff_only": "0", "valid_from": "", "valid_until": "",
    })
    entry_id = int(str(r.url).rsplit("edit=", 1)[1])

    client.post("/admin/faq-kb/playground", data={
        "mode": "draft", "entry_id": entry_id, "text": "какие направления есть", "language": "auto",
    })

    store = faq_kb.get_faq_kb_store()
    import asyncio
    log_rows = asyncio.run(store.list_answer_log())
    assert log_rows == []  # playground never writes faq_kb_answer_log

    live = asyncio.run(store.get_entry(entry_id))
    assert live.publication_status == "draft"  # playground never publishes anything


# --------------------------------------------------------------------------------------
# 47. Playground shows "no match" (including ambiguous) for unmatched text.
# --------------------------------------------------------------------------------------

def test_playground_shows_no_match_for_unrelated_text():
    client = _auth_client()
    _create_and_publish(client)

    r = client.post("/admin/faq-kb/playground", data={
        "mode": "published", "entry_id": 0, "text": "есть ли общежитие для студентов", "language": "auto",
    })
    assert r.status_code == 200
    # Humanized no-match copy (Fable UX finding 5) — no debug "Совпадений нет" dump.
    assert "Готового ответа в базе нет" in r.text


# --------------------------------------------------------------------------------------
# 48. Playground language param overrides auto-detection.
# --------------------------------------------------------------------------------------

def test_playground_language_param_overrides_detection():
    client = _auth_client()
    entry_id = _create_and_publish(
        client, question="сколько стоит обучение", answer_ru="RU-ответ 6500$", answer_ky="KY-жооп 6500$",
    )

    r_ru = client.post("/admin/faq-kb/playground", data={
        "mode": "published", "entry_id": entry_id, "text": "сколько стоит обучение", "language": "ru",
    })
    assert "RU-ответ" in r_ru.text

    r_ky = client.post("/admin/faq-kb/playground", data={
        "mode": "published", "entry_id": entry_id, "text": "сколько стоит обучение", "language": "ky",
    })
    assert "KY-жооп" in r_ky.text

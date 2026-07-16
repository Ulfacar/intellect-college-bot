"""Increment 8A — /admin-v2 (dev-only visual-redesign prototype).

Covers: (1) default-off isolation — the flag is False by default, so router_v2 is never
mounted onto the real app and /admin stays completely unaffected; (2) flag-on behavior —
login + shell + dialog list render 200 and reuse the SAME conversation store data as
/admin (both read app.integrations.panel.store.get_conversation_store(), a process
singleton, regardless of which FastAPI app wraps the router); (3) the dialog_owner
legacy-compat shim (bot/manager from `intercepted`, no false "paused" inference) and the
demo-only paused override; (4) the legacy 7-column kanban is present, labeled temporary,
and BOARD_COLUMNS itself is untouched; (5) the archive/unarchive P0 fix reuses the
EXISTING /admin/conversation/{id}/archive|unarchive routes and both backends' data agree.

Test #2 builds its own small FastAPI app that mirrors main.py's conditional-mount
pattern (`if settings.admin_ui_v2: app.include_router(router_v2)`) instead of reloading
app.main — app.main is a shared singleton imported by many other test modules, and
importlib.reload()-ing it in place would mutate that shared module object for the whole
pytest session. router_v2 itself is fully exercised either way.
"""
import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import app.main as main
from app.admin.router import BOARD_COLUMNS
from app.config import settings
from app.integrations.panel import store as panel_store


def _clear_memory():
    """Same cleanup as tests/test_panel.py — process-global in-memory stores."""
    panel_store._memory_store._conv.clear()
    panel_store._memory_store._audit.clear()
    from app.core.state import state_store
    state_store._store.clear()
    from app.admin import ratelimit
    ratelimit.reset()
    from app.core import flags
    flags.reset()


def _v2_app() -> FastAPI:
    """A standalone app that mirrors app/main.py's conditional mount of router_v2,
    for use once settings.admin_ui_v2 has been monkeypatched True. Does NOT touch the
    shared app.main module/app singleton."""
    from app.admin.router_v2 import router as admin_router_v2
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret,
                       max_age=14 * 24 * 3600, https_only=True, same_site="lax")
    app.include_router(admin_router_v2)
    return app


def _v2_client() -> TestClient:
    return TestClient(_v2_app(), base_url="https://testserver")


def _v2_auth_client() -> TestClient:
    client = _v2_client()
    r = client.post("/admin-v2/login", data={"login": "admin", "password": "change-me"})
    assert r.status_code == 200
    return client


def _old_auth_client() -> TestClient:
    client = TestClient(main.app, base_url="https://testserver")
    r = client.post("/admin/login", data={"login": "admin", "password": "change-me"})
    assert r.status_code == 200
    return client


def _seed(user_id: str, text: str, **meta):
    async def go():
        s = panel_store.get_conversation_store()
        await s.add_message(user_id, "client", text, channel="whatsapp", bot_id="college_1")
        await s.update_meta(user_id, funnel="admission", **meta)
    asyncio.run(go())


# ============================================================================
# (1) default off — router_v2 never mounted on the real app; /admin unaffected
# ============================================================================
def test_admin_v2_not_mounted_by_default():
    assert settings.admin_ui_v2 is False   # sanity: this is the shipped default
    client = TestClient(main.app, base_url="https://testserver")
    r = client.get("/admin-v2/login")
    assert r.status_code == 404


def test_admin_unaffected_when_v2_flag_off():
    _clear_memory()
    client = _old_auth_client()
    r = client.get("/admin")
    assert r.status_code == 200
    r = client.get("/admin/board/admission")
    assert r.status_code == 200
    _clear_memory()


# ============================================================================
# (2) flag on — login + shell + dialog list render 200, reuse the same store data
# ============================================================================
def test_admin_v2_enabled_login_and_board_reuse_same_data(monkeypatch):
    _clear_memory()
    monkeypatch.setattr(settings, "admin_ui_v2", True)
    _seed("college_1:v2_reuse_check", "Здравствуйте, хочу поступить", stage="greeting")

    v2 = _v2_auth_client()
    r = v2.get("/admin-v2")
    assert r.status_code == 200
    assert "v2 прототип" in r.text

    r = v2.get("/admin-v2/inbox")
    assert r.status_code == 200
    assert "Здравствуйте, хочу поступить" in r.text

    # Same underlying store singleton -> the classic /admin sees the identical card.
    old = _old_auth_client()
    r = old.get("/admin/board/admission")
    assert r.status_code == 200
    assert "Здравствуйте, хочу поступить" in r.text
    _clear_memory()


def test_admin_v2_login_wrong_password_rejected(monkeypatch):
    _clear_memory()
    monkeypatch.setattr(settings, "admin_ui_v2", True)
    client = _v2_client()
    r = client.post("/admin-v2/login", data={"login": "admin", "password": "wrong"})
    assert r.status_code == 401
    r = client.get("/admin-v2/inbox")
    assert r.status_code == 401
    _clear_memory()


# ============================================================================
# (3) dialog_owner: legacy compat shim (bot/manager) + demo-only paused override
# ============================================================================
def test_admin_v2_dialog_owner_shim_bot_and_manager(monkeypatch):
    _clear_memory()
    monkeypatch.setattr(settings, "admin_ui_v2", True)
    # "aaa_" sorts first -> demo-paused override lands here, not on the cases below.
    _seed("college_1:aaa_demo_paused_slot", "x", stage="greeting")
    _seed("college_1:bbb_bot_case", "клиент пишет", stage="greeting")
    _seed("college_1:ccc_manager_case", "клиент пишет менеджеру", stage="manager",
          intercepted=True, assigned_to="admin")

    v2 = _v2_auth_client()

    r = v2.get("/admin-v2/conversation/college_1:aaa_demo_paused_slot")
    assert r.status_code == 200
    assert "pill-paused" in r.text
    assert "Бот на паузе" in r.text

    r = v2.get("/admin-v2/conversation/college_1:bbb_bot_case")
    assert r.status_code == 200
    assert "pill-bot" in r.text
    assert "Бот отвечает" in r.text

    r = v2.get("/admin-v2/conversation/college_1:ccc_manager_case")
    assert r.status_code == 200
    assert "pill-manager" in r.text
    assert "Менеджер · admin" in r.text
    _clear_memory()


# ============================================================================
# (4) legacy kanban: labeled temporary, BOARD_COLUMNS itself untouched
# ============================================================================
def test_admin_v2_legacy_kanban_labeled_temporary_columns_unchanged(monkeypatch):
    assert [key for key, _ in BOARD_COLUMNS] == [
        "greeting", "qualification", "progress", "office", "manager", "silent", "follow_up",
    ]
    _clear_memory()
    monkeypatch.setattr(settings, "admin_ui_v2", True)
    _seed("college_1:kanban_check", "тест канбана", stage="greeting")
    v2 = _v2_auth_client()
    r = v2.get("/admin-v2/board/admission")
    assert r.status_code == 200
    assert "legacy-board" in r.text
    assert "временный" in r.text
    assert "kanban_check" in r.text   # the seeded card (shown as name — no phone/qualification set)
    _clear_memory()


# ============================================================================
# (5) archive/unarchive: reuses EXISTING /admin/conversation/{id}/archive|unarchive
# ============================================================================
def test_admin_v2_archive_section_reuses_existing_admin_routes(monkeypatch):
    _clear_memory()
    monkeypatch.setattr(settings, "admin_ui_v2", True)
    _seed("college_1:archive_flow", "архивный тест", stage="greeting")

    v2 = _v2_auth_client()
    old = _old_auth_client()

    r = v2.get("/admin-v2/archive")
    assert r.status_code == 200 and "архивный тест" not in r.text

    r = old.post("/admin/conversation/college_1:archive_flow/archive")
    assert r.status_code == 200 and r.json()["ok"] is True

    r = v2.get("/admin-v2/archive")
    assert r.status_code == 200 and "архивный тест" in r.text
    r = v2.get("/admin-v2/inbox")
    assert "архивный тест" not in r.text

    r = old.post("/admin/conversation/college_1:archive_flow/unarchive")
    assert r.status_code == 200 and r.json()["ok"] is True
    r = v2.get("/admin-v2/archive")
    assert "архивный тест" not in r.text
    _clear_memory()

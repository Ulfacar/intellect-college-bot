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
from app.core import telegram_sessions
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


# ============================================================================
# Increment 8B — routing extras: environment does NOT implicitly flip admin_ui_v2
# ============================================================================
def test_admin_v2_environment_production_does_not_auto_enable_v2(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    assert settings.admin_ui_v2 is False   # unrelated flags — production alone changes nothing
    client = TestClient(main.app, base_url="https://testserver")
    r = client.get("/admin-v2/login")
    assert r.status_code == 404


# ============================================================================
# Increment 8B R8 (owner §11) — demo-login prod safety, the one authorized backend gate
# ============================================================================
def test_demo_login_available_helper(monkeypatch):
    monkeypatch.setattr(settings, "demo_login", True)
    monkeypatch.setattr(settings, "environment", "dev")
    assert settings.demo_login_available() is True
    monkeypatch.setattr(settings, "environment", "production")
    assert settings.demo_login_available() is False
    monkeypatch.setattr(settings, "demo_login", False)
    monkeypatch.setattr(settings, "environment", "dev")
    assert settings.demo_login_available() is False


def test_classic_admin_demo_login_force_disabled_in_production(monkeypatch):
    _clear_memory()
    monkeypatch.setattr(settings, "demo_login", True)
    monkeypatch.setattr(settings, "environment", "dev")
    client = TestClient(main.app, base_url="https://testserver")
    r = client.get("/admin/login")
    assert "Быстрый вход" in r.text   # demo buttons shown outside production

    monkeypatch.setattr(settings, "environment", "production")
    r = client.get("/admin/login")
    assert "Быстрый вход" not in r.text   # force-hidden in production
    r = client.post("/admin/login/demo", data={"login": "admin"})
    assert r.status_code == 404
    _clear_memory()


def test_v2_demo_login_force_disabled_in_production(monkeypatch):
    _clear_memory()
    monkeypatch.setattr(settings, "admin_ui_v2", True)
    monkeypatch.setattr(settings, "demo_login", True)
    monkeypatch.setattr(settings, "environment", "dev")
    client = _v2_client()
    r = client.get("/admin-v2/login")
    assert "Быстрый вход" in r.text
    r = client.post("/admin-v2/login/demo", data={"login": "admin"})
    assert r.status_code == 200   # allowed outside production (redirect auto-followed to /admin-v2)

    monkeypatch.setattr(settings, "environment", "production")
    client2 = _v2_client()
    r = client2.get("/admin-v2/login")
    assert "Быстрый вход" not in r.text
    r = client2.post("/admin-v2/login/demo", data={"login": "admin"})
    assert r.status_code == 404
    _clear_memory()


# ============================================================================
# Increment 8B R5 — inbox filters (scope: waiting/all, owner: bot/manager/paused)
# ============================================================================
def test_inbox_filters_by_owner_and_scope(monkeypatch):
    _clear_memory()
    monkeypatch.setattr(settings, "admin_ui_v2", True)
    # "aaa_" sorts first -> absorbs the demo-paused override (see _demo_paused_user_id).
    _seed("college_1:aaa_paused_slot", "пауза-заглушка", stage="greeting")
    _seed("college_1:bbb_bot_waiting", "клиент ждёт бота", stage="greeting")
    _seed("college_1:ccc_manager_waiting", "клиент ждёт менеджера", stage="manager",
          intercepted=True, assigned_to="admin")

    v2 = _v2_auth_client()

    r = v2.get("/admin-v2/inbox?owner=paused")
    assert r.status_code == 200
    assert "aaa_paused_slot" in r.text
    assert "bbb_bot_waiting" not in r.text and "ccc_manager_waiting" not in r.text

    r = v2.get("/admin-v2/inbox?owner=bot")
    assert "bbb_bot_waiting" in r.text
    assert "aaa_paused_slot" not in r.text and "ccc_manager_waiting" not in r.text

    r = v2.get("/admin-v2/inbox?owner=manager")
    assert "ccc_manager_waiting" in r.text
    assert "bbb_bot_waiting" not in r.text and "aaa_paused_slot" not in r.text
    _clear_memory()


def test_inbox_scope_all_shows_non_waiting_active_conversations(monkeypatch):
    _clear_memory()
    monkeypatch.setattr(settings, "admin_ui_v2", True)
    v2 = _v2_auth_client()

    async def go():
        s = panel_store.get_conversation_store()
        await s.add_message("college_1:already_answered", "client", "вопрос", channel="whatsapp", bot_id="college_1")
        await s.add_message("college_1:already_answered", "bot", "ответ бота", channel="whatsapp", bot_id="college_1")
        await s.update_meta("college_1:already_answered", funnel="admission", stage="greeting")
    asyncio.run(go())

    r = v2.get("/admin-v2/inbox")   # default scope=waiting — last message was FROM the bot, not waiting
    assert r.status_code == 200
    assert "already_answered" not in r.text

    r = v2.get("/admin-v2/inbox?scope=all")
    assert "already_answered" in r.text
    _clear_memory()


# ============================================================================
# Increment 8B R3 — ownership: takeover/release never touch stage/outcome; a manager
# with NO assigned_to is still 'manager', never inferred as 'paused' (owner §1).
# ============================================================================
def test_takeover_and_release_do_not_change_lead_stage_or_outcome(monkeypatch):
    _clear_memory()
    monkeypatch.setattr(settings, "admin_ui_v2", True)
    # "aaa_" sorts first -> absorbs the demo-paused override (see _demo_paused_user_id),
    # so it doesn't mask the real bot/manager pill on the conversation under test.
    _seed("college_1:aaa_decoy", "x", stage="greeting")
    _seed("college_1:ownership_flow", "нужна консультация", stage="qualification", outcome="in_progress")

    v2 = _v2_auth_client()
    old = _old_auth_client()

    r = old.post("/admin/conversation/college_1:ownership_flow/takeover")
    assert r.status_code == 200

    async def read():
        return await panel_store.get_conversation_store().get("college_1:ownership_flow")
    conv = asyncio.run(read())
    assert conv.stage == "qualification" and conv.outcome == "in_progress"
    assert conv.intercepted is True

    r = v2.get("/admin-v2/conversation/college_1:ownership_flow")
    assert "pill-manager" in r.text

    r = old.post("/admin/conversation/college_1:ownership_flow/release")
    assert r.status_code == 200
    conv = asyncio.run(read())
    assert conv.stage == "qualification" and conv.outcome == "in_progress"
    assert conv.intercepted is False
    _clear_memory()


def test_manager_without_assigned_to_not_shown_as_paused(monkeypatch):
    _clear_memory()
    monkeypatch.setattr(settings, "admin_ui_v2", True)
    # "aaa_" sorts first among the two seeded ids -> demo-paused override lands on the
    # OTHER conversation, not this one, so this row exercises the real shim, not the demo.
    _seed("college_1:aaa_decoy", "x", stage="greeting")
    _seed("college_1:zzz_no_assignee", "клиент пишет", stage="manager", intercepted=True, assigned_to="")

    v2 = _v2_auth_client()
    r = v2.get("/admin-v2/conversation/college_1:zzz_no_assignee")
    assert r.status_code == 200
    assert "pill-manager" in r.text
    assert "pill-paused" not in r.text
    assert "Менеджер не назначен" in r.text
    _clear_memory()


# ============================================================================
# Increment 8B R2 — manual reply reuses the EXISTING /admin/.../send route; works
# regardless of OpenRouter budget (the route never touches app.core.budget at all).
# ============================================================================
def test_manual_reply_reuses_existing_send_route_and_ignores_budget(monkeypatch):
    _clear_memory()
    monkeypatch.setattr(settings, "admin_ui_v2", True)
    monkeypatch.setattr(settings, "llm_daily_budget_usd", 0.000001)   # effectively exhausted

    async def fake_send(channel, bot_id, chat_id, text):
        return "prov-1"
    monkeypatch.setattr("app.channels.outbound.send_to_client", fake_send)
    # "aaa_" sorts first -> absorbs the demo-paused override (see _demo_paused_user_id).
    _seed("college_1:aaa_decoy", "x", stage="greeting")
    _seed("college_1:reply_flow", "здравствуйте", stage="greeting")

    old = _old_auth_client()
    r = old.post("/admin/conversation/college_1:reply_flow/send", data={"text": "Добрый день!"})
    assert r.status_code == 200

    v2 = _v2_auth_client()
    r = v2.get("/admin-v2/conversation/college_1:reply_flow")
    assert "Добрый день!" in r.text
    assert "pill-manager" in r.text   # manual send auto-takes-over, per the existing route
    _clear_memory()


def test_composer_omitted_for_archived_conversation_in_v2_workspace(monkeypatch):
    _clear_memory()
    monkeypatch.setattr(settings, "admin_ui_v2", True)
    _seed("college_1:archived_no_composer", "тест", stage="greeting")

    old = _old_auth_client()
    r = old.post("/admin/conversation/college_1:archived_no_composer/archive")
    assert r.status_code == 200

    v2 = _v2_auth_client()
    r = v2.get("/admin-v2/conversation/college_1:archived_no_composer")
    assert r.status_code == 200
    assert '<form class="composer"' not in r.text
    assert "восстановите" in r.text.lower()
    _clear_memory()


# ============================================================================
# Increment 8B R4 — archive: bulk archive + idempotent repeat archive
# ============================================================================
def test_bulk_archive_via_existing_route_visible_in_v2_archive(monkeypatch):
    _clear_memory()
    monkeypatch.setattr(settings, "admin_ui_v2", True)
    _seed("college_1:bulk_a", "a", stage="greeting")
    _seed("college_1:bulk_b", "b", stage="greeting")

    old = _old_auth_client()
    # httpx's `data=` needs a dict with a LIST value to encode repeated form fields —
    # a list of (key, value) tuples silently fails to serialize (httpx, unlike
    # `requests`, does not support that shape) and the earlier version of this test
    # posted an effectively-empty body, archiving nothing while still returning 200.
    r = old.post("/admin/conversations/archive",
                data={"user_ids": ["college_1:bulk_a", "college_1:bulk_b"]})
    assert r.status_code == 200

    v2 = _v2_auth_client()
    r = v2.get("/admin-v2/archive")
    assert "bulk_a" in r.text and "bulk_b" in r.text
    _clear_memory()


def test_repeat_archive_is_idempotent(monkeypatch):
    _clear_memory()
    monkeypatch.setattr(settings, "admin_ui_v2", True)
    _seed("college_1:idempotent_archive", "x", stage="greeting")
    old = _old_auth_client()
    r1 = old.post("/admin/conversation/college_1:idempotent_archive/archive")
    r2 = old.post("/admin/conversation/college_1:idempotent_archive/archive")
    assert r1.status_code == 200 and r1.json()["ok"] is True
    assert r2.status_code == 200 and r2.json()["ok"] is True
    _clear_memory()


# ============================================================================
# Increment 8B R6 — system page: effective = global AND individual, secrets never shown
# ============================================================================
def test_system_page_effective_flag_and_budget_and_no_secrets(monkeypatch):
    _clear_memory()
    monkeypatch.setattr(settings, "admin_ui_v2", True)
    v2 = _v2_auth_client()

    from app.core import flags

    async def set_flags():
        await flags.set_flag("bots_enabled", True)
        await flags.set_flag("bots_enabled:college_1", True)
    asyncio.run(set_flags())
    r = v2.get("/admin-v2/system")
    assert r.status_code == 200
    assert "ОТВЕЧАЕТ" in r.text

    async def turn_off_individual():
        await flags.set_flag("bots_enabled:college_1", False)
    asyncio.run(turn_off_individual())
    r = v2.get("/admin-v2/system")
    assert "МОЛЧИТ" in r.text
    assert "выключен индивидуально" in r.text

    async def global_off_wins():
        await flags.set_flag("bots_enabled:college_1", True)   # individual ON again
        await flags.set_flag("bots_enabled", False)             # but GLOBAL off
    asyncio.run(global_off_wins())
    r = v2.get("/admin-v2/system")
    assert "МОЛЧИТ" in r.text   # effective = global AND individual -> global OFF wins
    assert "выключен главный рубильник" in r.text

    # Secrets must NEVER leak into the rendered page.
    assert settings.admin_password not in r.text
    assert "change-me" not in r.text  # default admin_password/session_secret fixture value
    _clear_memory()


def test_system_page_shows_budget_exhausted(monkeypatch):
    _clear_memory()
    monkeypatch.setattr(settings, "admin_ui_v2", True)
    # A tiny-but-nonzero daily budget makes `reserve()`'s own worst-case estimate exceed
    # the limit on the FIRST call, so it refuses without ever inserting an ai_answer_log
    # row (see app/core/budget.py::reserve) — real committed spend would stay 0 and
    # is_exhausted() (a cheap read of COMMITTED spend only) would then report False. That
    # accounting nuance belongs to tests/test_budget.py, not this view test, so here we
    # patch the router's `is_exhausted` call directly to assert only what R6 promises:
    # the system page renders whatever BudgetStatus it's given.
    from app.core import budget as budget_mod

    async def fake_exhausted(*, bot_id=None, now=None):
        return budget_mod.BudgetStatus(True, "daily_exceeded", 5.0, 5.0, 12.0, 50.0)
    monkeypatch.setattr(budget_mod, "is_exhausted", fake_exhausted)

    v2 = _v2_auth_client()
    r = v2.get("/admin-v2/system")
    assert r.status_code == 200
    assert "бюджет исчерпан" in r.text
    _clear_memory()


# ============================================================================
# Increment 8B R1/R7 — toast layer + doAction wrapper + mobile CSS states present
# ============================================================================
def test_shell_has_toast_layer_doaction_and_mobile_css(monkeypatch):
    _clear_memory()
    monkeypatch.setattr(settings, "admin_ui_v2", True)
    v2 = _v2_auth_client()
    r = v2.get("/admin-v2")
    assert r.status_code == 200
    assert 'id="toast-stack"' in r.text
    assert "function doAction" in r.text
    assert "function toast(" in r.text
    assert "htmx:responseError" in r.text
    # mobile states (owner §9)
    assert "@media (max-width: 860px)" in r.text
    assert "mobile-chat-open" in r.text
    assert "env(safe-area-inset-bottom)" in r.text
    assert "btn-back" in r.text
    _clear_memory()


# ============================================================================
# Increment 8B §8 — server-side manual-reply safety. Both /admin and /admin-v2 POST the
# SAME `/admin/conversation/{id}/send` route, so one server guard protects both UIs:
# archived -> reject, stale/old-tab session -> 409, valid active session -> reply sent;
# a rejected reply performs NO external send and writes nothing; the guard never depends
# on OpenRouter budget and never touches lead_status.
# ============================================================================
def _both_app() -> FastAPI:
    """A standalone app mounting BOTH the classic /admin router and the /admin-v2 router
    (mirrors app/main.py when admin_ui_v2 is on) so we can prove the SAME send route
    guards a request authenticated through the v2 login as through the classic login."""
    from app.admin.router import router as admin_router
    from app.admin.router_v2 import router as admin_router_v2
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret,
                       max_age=14 * 24 * 3600, https_only=True, same_site="lax")
    app.include_router(admin_router)
    app.include_router(admin_router_v2)
    return app


def _reset_pilot_sessions():
    """Clear the process-global in-memory lead/conversation store + session locks so each
    §8 test starts with no active Telegram-pilot session (complements _clear_memory,
    which only touches the legacy panel store)."""
    from app.integrations.panel import leadstore
    leadstore._memory_lead_store._leads.clear()
    leadstore._memory_lead_store._conversations.clear()
    leadstore._memory_lead_store._lead_seq = 0
    leadstore._memory_lead_store._conv_seq = 0
    telegram_sessions._locks.clear()


def _active_session(bot_id: str, ext: str) -> str:
    async def go():
        return await telegram_sessions.ensure_active_session(bot_id, ext, external_chat_id=ext)
    return asyncio.run(go()).conversation.session_id


def _newtest_session(bot_id: str, ext: str) -> str:
    async def go():
        return await telegram_sessions.start_new_session(bot_id, ext, external_chat_id=ext)
    return asyncio.run(go()).conversation.session_id


def _spy_send(monkeypatch) -> list:
    sent: list = []
    async def fake_send(channel, bot_id, chat_id, text):
        sent.append(text)
        return "prov-spy"
    monkeypatch.setattr("app.channels.outbound.send_to_client", fake_send)
    return sent


# (1) Нельзя ответить в archived Conversation — 409, без внешней отправки.
def test_send_rejected_when_conversation_archived(monkeypatch):
    _clear_memory(); _reset_pilot_sessions()
    sent = _spy_send(monkeypatch)
    _seed("college_1:s8_archived", "здравствуйте", stage="greeting")

    old = _old_auth_client()
    assert old.post("/admin/conversation/college_1:s8_archived/archive").status_code == 200

    r = old.post("/admin/conversation/college_1:s8_archived/send", data={"text": "ответ"})
    assert r.status_code == 409
    assert sent == []   # §8: no external send on a rejected reply
    _clear_memory(); _reset_pilot_sessions()


# (2) Нельзя ответить в stale session — активна одна сессия, вкладка шлёт другой id → 409.
def test_send_rejected_when_session_is_stale(monkeypatch):
    _clear_memory(); _reset_pilot_sessions()
    sent = _spy_send(monkeypatch)
    _active_session("college_1", "s8_stale")      # current active pilot session
    _seed("college_1:s8_stale", "вопрос", stage="greeting")

    old = _old_auth_client()
    r = old.post("/admin/conversation/college_1:s8_stale/send",
                 data={"text": "ответ", "expected_session_id": "some-older-session-id"})
    assert r.status_code == 409
    assert sent == []
    _clear_memory(); _reset_pilot_sessions()


# (3) Старая вкладка после /newtest — вкладка держит id старой сессии → 409.
def test_send_rejected_from_old_tab_after_newtest(monkeypatch):
    _clear_memory(); _reset_pilot_sessions()
    sent = _spy_send(monkeypatch)
    old_session = _active_session("college_1", "s8_newtest")
    _seed("college_1:s8_newtest", "вопрос", stage="greeting")
    new_session = _newtest_session("college_1", "s8_newtest")   # tester ran /newtest
    assert new_session != old_session

    old = _old_auth_client()
    r = old.post("/admin/conversation/college_1:s8_newtest/send",
                 data={"text": "ответ", "expected_session_id": old_session})
    assert r.status_code == 409
    assert sent == []
    _clear_memory(); _reset_pilot_sessions()


# (4) Валидная активная Conversation — reply уходит; бюджет игнорируется; lead_status не меняется.
def test_send_allowed_for_current_active_session(monkeypatch):
    _clear_memory(); _reset_pilot_sessions()
    monkeypatch.setattr(settings, "llm_daily_budget_usd", 0.000001)  # бюджет фактически исчерпан
    sent = _spy_send(monkeypatch)
    active_id = _active_session("college_1", "s8_ok")
    _seed("college_1:s8_ok", "здравствуйте", stage="greeting")

    old = _old_auth_client()
    r = old.post("/admin/conversation/college_1:s8_ok/send",
                 data={"text": "Добрый день!", "expected_session_id": active_id})
    assert r.status_code == 200
    assert sent == ["Добрый день!"]   # внешняя отправка выполнена несмотря на исчерпанный бюджет

    async def read_conv():
        return await panel_store.get_conversation_store().get("college_1:s8_ok")
    conv = asyncio.run(read_conv())
    assert conv.intercepted is True   # ручная отправка авто-перехватывает (существующее поведение)

    async def read_lead():
        from app.integrations.panel import leadstore
        store = leadstore.get_lead_store()
        c = await store.get_active_conversation("college_1", "s8_ok")
        return await store.get_lead(c.lead_id)
    lead = asyncio.run(read_lead())
    assert lead.lead_status == "new"   # §8: manual reply НЕ трогает lead_status
    _clear_memory(); _reset_pilot_sessions()


# (5) Ошибка (rejected send) не вызывает внешнюю отправку и ничего не пишет в диалог.
def test_rejected_send_writes_nothing_and_sends_nothing(monkeypatch):
    _clear_memory(); _reset_pilot_sessions()
    sent = _spy_send(monkeypatch)
    _active_session("college_1", "s8_noop")
    _seed("college_1:s8_noop", "вопрос", stage="greeting")

    old = _old_auth_client()
    r = old.post("/admin/conversation/college_1:s8_noop/send",
                 data={"text": "не должно уйти", "expected_session_id": "stale-xyz"})
    assert r.status_code == 409
    assert sent == []

    async def read_conv():
        return await panel_store.get_conversation_store().get("college_1:s8_noop")
    conv = asyncio.run(read_conv())
    assert conv.intercepted is False                                   # перехвата не было
    assert all(m.sender != "manager" for m in conv.messages)           # pending-сообщение не создано
    assert "не должно уйти" not in [m.text for m in conv.messages]
    _clear_memory(); _reset_pilot_sessions()


# (6) Старый и новый UI используют одинаковую серверную защиту (один и тот же маршрут).
def test_send_guard_identical_for_old_and_v2_ui(monkeypatch):
    _clear_memory(); _reset_pilot_sessions()
    monkeypatch.setattr(settings, "admin_ui_v2", True)
    sent = _spy_send(monkeypatch)
    _active_session("college_1", "s8_both")
    _seed("college_1:s8_both", "вопрос", stage="greeting")

    # classic /admin login → shared send route → stale → 409
    old = _old_auth_client()
    r_old = old.post("/admin/conversation/college_1:s8_both/send",
                     data={"text": "x", "expected_session_id": "stale"})
    assert r_old.status_code == 409

    # v2 login (same session-cookie mechanism) on a combined app → SAME send route → 409
    both = TestClient(_both_app(), base_url="https://testserver")
    assert both.post("/admin-v2/login",
                     data={"login": "admin", "password": "change-me"}).status_code == 200
    r_v2 = both.post("/admin/conversation/college_1:s8_both/send",
                     data={"text": "x", "expected_session_id": "stale"})
    assert r_v2.status_code == 409

    assert sent == []   # ни один UI ничего не отправил на отклонённом ответе
    _clear_memory(); _reset_pilot_sessions()


# ============================================================================
# Increment 8B §8 (owner follow-up) — /resend/{id} проходит ТОТ ЖЕ серверный guard,
# что и ручной ответ: archived/stale отклоняются ДО внешней отправки, без записи.
# ============================================================================
def _seed_failed_message(user_id: str, text: str = "повтор", channel: str = "whatsapp") -> int:
    """Диалог с одним failed manager-сообщением (кандидат на resend). Возвращает его id."""
    async def go():
        s = panel_store.get_conversation_store()
        await s.add_message(user_id, "client", "вопрос", channel=channel, bot_id="college_1")
        mid = await s.add_message(user_id, "manager", text, channel=channel,
                                  bot_id="college_1", status="failed")
        await s.update_meta(user_id, funnel="admission", stage="greeting")
        return mid
    return asyncio.run(go())


# (1) resend запрещён для archived Conversation.
def test_resend_rejected_when_conversation_archived(monkeypatch):
    _clear_memory(); _reset_pilot_sessions()
    sent = _spy_send(monkeypatch)
    mid = _seed_failed_message("college_1:rs_archived")

    old = _old_auth_client()
    assert old.post("/admin/conversation/college_1:rs_archived/archive").status_code == 200
    r = old.post("/admin/conversation/college_1:rs_archived/resend/" + str(mid))
    assert r.status_code == 409
    assert sent == []
    _clear_memory(); _reset_pilot_sessions()


# (2) resend запрещён для stale session.
def test_resend_rejected_when_session_is_stale(monkeypatch):
    _clear_memory(); _reset_pilot_sessions()
    sent = _spy_send(monkeypatch)
    _active_session("college_1", "rs_stale")
    mid = _seed_failed_message("college_1:rs_stale")

    old = _old_auth_client()
    r = old.post("/admin/conversation/college_1:rs_stale/resend/" + str(mid),
                 data={"expected_session_id": "some-older-session-id"})
    assert r.status_code == 409
    assert sent == []
    _clear_memory(); _reset_pilot_sessions()


# (3) resend из старой вкладки после /newtest ничего не отправляет.
def test_resend_rejected_from_old_tab_after_newtest(monkeypatch):
    _clear_memory(); _reset_pilot_sessions()
    sent = _spy_send(monkeypatch)
    old_session = _active_session("college_1", "rs_newtest")
    mid = _seed_failed_message("college_1:rs_newtest")
    new_session = _newtest_session("college_1", "rs_newtest")
    assert new_session != old_session

    old = _old_auth_client()
    r = old.post("/admin/conversation/college_1:rs_newtest/resend/" + str(mid),
                 data={"expected_session_id": old_session})
    assert r.status_code == 409
    assert sent == []
    _clear_memory(); _reset_pilot_sessions()


# (4) валидный failed message в активной session можно отправить повторно.
def test_resend_allowed_for_current_active_session(monkeypatch):
    _clear_memory(); _reset_pilot_sessions()
    sent = _spy_send(monkeypatch)
    active_id = _active_session("college_1", "rs_ok")
    mid = _seed_failed_message("college_1:rs_ok", text="повторяю ответ")

    old = _old_auth_client()
    r = old.post("/admin/conversation/college_1:rs_ok/resend/" + str(mid),
                 data={"expected_session_id": active_id})
    assert r.status_code == 200
    assert sent == ["повторяю ответ"]   # повтор ушёл только после успешного guard

    async def read_msg():
        conv = await panel_store.get_conversation_store().get("college_1:rs_ok")
        return next(m for m in conv.messages if m.id == mid)
    assert asyncio.run(read_msg()).status == "sent"
    _clear_memory(); _reset_pilot_sessions()


# (5) при отклонении resend transport.send не вызывается (сообщение остаётся failed).
def test_rejected_resend_does_not_call_transport(monkeypatch):
    _clear_memory(); _reset_pilot_sessions()
    sent = _spy_send(monkeypatch)
    _active_session("college_1", "rs_noop")
    mid = _seed_failed_message("college_1:rs_noop")

    old = _old_auth_client()
    r = old.post("/admin/conversation/college_1:rs_noop/resend/" + str(mid),
                 data={"expected_session_id": "stale-xyz"})
    assert r.status_code == 409
    assert sent == []

    async def read_msg():
        conv = await panel_store.get_conversation_store().get("college_1:rs_noop")
        return next(m for m in conv.messages if m.id == mid)
    assert asyncio.run(read_msg()).status == "failed"   # состояние сообщения не изменилось
    _clear_memory(); _reset_pilot_sessions()


# ============================================================================
# Increment 8B §8 (owner fail-closed rule) — если Conversation session-aware (Telegram
# pilot), но активную сессию проверить НЕВОЗМОЖНО (сбой lookup) — и send, и resend
# отклоняются с 503 без внешней отправки. Legacy-диалоги (без session-модели) не задеты.
# ============================================================================
def test_failed_session_lookup_fails_closed_for_pilot_send_and_resend(monkeypatch):
    _clear_memory(); _reset_pilot_sessions()
    sent = _spy_send(monkeypatch)

    async def boom(*a, **k):
        raise RuntimeError("session backend down")
    monkeypatch.setattr(telegram_sessions, "get_active_session", boom)

    # Telegram-pilot диалог: channel="telegram" + ключ "<bot_id>:<ext>" → session-aware.
    async def seed_pilot():
        s = panel_store.get_conversation_store()
        await s.add_message("college_1:fc_pilot", "client", "вопрос", channel="telegram", bot_id="college_1")
        mid = await s.add_message("college_1:fc_pilot", "manager", "повтор", channel="telegram",
                                  bot_id="college_1", status="failed")
        await s.update_meta("college_1:fc_pilot", funnel="admission", stage="greeting")
        return mid
    mid = asyncio.run(seed_pilot())

    old = _old_auth_client()
    r_send = old.post("/admin/conversation/college_1:fc_pilot/send", data={"text": "привет"})
    assert r_send.status_code == 503
    r_resend = old.post("/admin/conversation/college_1:fc_pilot/resend/" + str(mid))
    assert r_resend.status_code == 503
    assert sent == []   # fail closed — внешней отправки не было ни в одном пути

    # Контроль: legacy-диалог (channel="whatsapp") при том же сбое lookup остаётся
    # доступен (fail open) — у него объективно нет session-модели.
    _seed("college_1:fc_legacy", "здравствуйте", stage="greeting")
    r_legacy = old.post("/admin/conversation/college_1:fc_legacy/send", data={"text": "ответ"})
    assert r_legacy.status_code == 200
    assert sent == ["ответ"]
    _clear_memory(); _reset_pilot_sessions()


# ============================================================================
# Increment 8B §8 (owner follow-up) — resend разрешён ТОЛЬКО для outgoing failed-
# сообщения, принадлежащего ЭТОЙ Conversation. Проверка серверная (не UI-only).
# ============================================================================
def _seed_outgoing_message(user_id: str, status: str, text: str = "ответ") -> int:
    """Диалог с одним исходящим manager-сообщением заданного статуса. Возвращает его id."""
    async def go():
        s = panel_store.get_conversation_store()
        await s.add_message(user_id, "client", "вопрос", channel="whatsapp", bot_id="college_1")
        mid = await s.add_message(user_id, "manager", text, channel="whatsapp",
                                  bot_id="college_1", status=status)
        await s.update_meta(user_id, funnel="admission", stage="greeting")
        return mid
    return asyncio.run(go())


def _message_status(user_id: str, mid: int) -> str:
    async def go():
        conv = await panel_store.get_conversation_store().get(user_id)
        return next(m for m in conv.messages if m.id == mid).status
    return asyncio.run(go())


# (1) resend сообщения со status=sent отклоняется без transport.send.
def test_resend_rejected_for_sent_message(monkeypatch):
    _clear_memory(); _reset_pilot_sessions()
    sent = _spy_send(monkeypatch)
    mid = _seed_outgoing_message("college_1:rs_sent", "sent")

    old = _old_auth_client()
    r = old.post("/admin/conversation/college_1:rs_sent/resend/" + str(mid))
    assert r.status_code == 409
    assert sent == []
    assert _message_status("college_1:rs_sent", mid) == "sent"   # статус не изменился
    _clear_memory(); _reset_pilot_sessions()


# (2) resend сообщения со status=delivered отклоняется.
def test_resend_rejected_for_delivered_message(monkeypatch):
    _clear_memory(); _reset_pilot_sessions()
    sent = _spy_send(monkeypatch)
    mid = _seed_outgoing_message("college_1:rs_delivered", "delivered")

    old = _old_auth_client()
    r = old.post("/admin/conversation/college_1:rs_delivered/resend/" + str(mid))
    assert r.status_code == 409
    assert sent == []
    assert _message_status("college_1:rs_delivered", mid) == "delivered"
    _clear_memory(); _reset_pilot_sessions()


# (3) resend чужого message_id из другой Conversation отклоняется.
def test_resend_rejects_message_from_another_conversation(monkeypatch):
    _clear_memory(); _reset_pilot_sessions()
    sent = _spy_send(monkeypatch)
    _seed_failed_message("college_1:rs_convA")
    mid_b = _seed_failed_message("college_1:rs_convB")   # failed-сообщение принадлежит B

    old = _old_auth_client()
    # пытаемся повторить сообщение B через Conversation A
    r = old.post("/admin/conversation/college_1:rs_convA/resend/" + str(mid_b))
    assert r.status_code == 404
    assert sent == []
    assert _message_status("college_1:rs_convB", mid_b) == "failed"   # B не тронут
    _clear_memory(); _reset_pilot_sessions()


# (4) failed Telegram-pilot message в активной session успешно отправляется повторно.
def test_resend_allowed_for_failed_pilot_message_in_active_session(monkeypatch):
    _clear_memory(); _reset_pilot_sessions()
    sent = _spy_send(monkeypatch)
    active_id = _active_session("college_1", "rs_pilot")

    async def seed_pilot():
        s = panel_store.get_conversation_store()
        await s.add_message("college_1:rs_pilot", "client", "вопрос", channel="telegram", bot_id="college_1")
        mid = await s.add_message("college_1:rs_pilot", "manager", "повтор ответа", channel="telegram",
                                  bot_id="college_1", status="failed")
        await s.update_meta("college_1:rs_pilot", funnel="admission", stage="greeting")
        return mid
    mid = asyncio.run(seed_pilot())

    old = _old_auth_client()
    r = old.post("/admin/conversation/college_1:rs_pilot/resend/" + str(mid),
                 data={"expected_session_id": active_id})
    assert r.status_code == 200
    assert sent == ["повтор ответа"]
    assert _message_status("college_1:rs_pilot", mid) == "sent"
    _clear_memory(); _reset_pilot_sessions()


# (доп.) resend ВХОДЯЩЕГО (client) сообщения отклоняется — «не исходящее» (требование №3).
def test_resend_rejected_for_incoming_client_message(monkeypatch):
    _clear_memory(); _reset_pilot_sessions()
    sent = _spy_send(monkeypatch)
    # первое сообщение диалога — входящее от клиента (sender="client", status="").
    async def go():
        s = panel_store.get_conversation_store()
        mid = await s.add_message("college_1:rs_incoming", "client", "вопрос",
                                  channel="whatsapp", bot_id="college_1")
        await s.update_meta("college_1:rs_incoming", funnel="admission", stage="greeting")
        return mid
    mid = asyncio.run(go())

    old = _old_auth_client()
    r = old.post("/admin/conversation/college_1:rs_incoming/resend/" + str(mid))
    assert r.status_code == 409
    assert sent == []
    _clear_memory(); _reset_pilot_sessions()


# (доп.) resend сообщения со status=pending отклоняется (ещё отправляется — не повтор).
def test_resend_rejected_for_pending_message(monkeypatch):
    _clear_memory(); _reset_pilot_sessions()
    sent = _spy_send(monkeypatch)
    mid = _seed_outgoing_message("college_1:rs_pending", "pending")

    old = _old_auth_client()
    r = old.post("/admin/conversation/college_1:rs_pending/resend/" + str(mid))
    assert r.status_code == 409
    assert sent == []
    assert _message_status("college_1:rs_pending", mid) == "pending"
    _clear_memory(); _reset_pilot_sessions()

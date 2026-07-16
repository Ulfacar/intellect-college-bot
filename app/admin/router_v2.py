"""Increment 8A — /admin-v2 visual-redesign PROTOTYPE (dev-only, view-only).

Presentation only. This router does NOT talk to OpenRouter/FAQ/LeadStatusService, does
NOT touch Conversation/Lead stores' write paths beyond what app/admin/router.py already
exposes, and does NOT change routing/migrations/dialog_owner semantics. It:

  * REUSES app.admin.router's data helpers (`_build_board`, `_card_model`, `_now`,
    `BOARD_COLUMNS`, `OUTCOMES`, `QUALIFICATION_LABELS`, `_qualification_rows`,
    `_initials`, `_avatar`, `FUNNELS`, `current_manager`/`require_admin`,
    `_check_credentials`, `_demo_managers`) instead of duplicating any business logic.
  * For every WRITE action (takeover/release/send/resend/outcome/stage/archive/
    unarchive/suggest) the v2 templates/JS call the EXISTING `/admin/...` endpoints in
    app/admin/router.py directly (see v2/_style.html's inline script) — router_v2 adds
    NO new write endpoints of its own beyond the login/logout session dance, which is
    the same session mechanism `current_manager()`/`require_admin` already read.
  * Renders NEW templates under app/admin/templates/v2/ via a dedicated Jinja2Templates
    instance (own `directory=`), so /admin's templates are never touched.

Mounted at /admin-v2 ONLY when settings.admin_ui_v2 is true (see app/main.py) — off by
default, so /admin and the test suite are unaffected.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.admin.router import (
    BOARD_COLUMNS,
    FUNNELS,
    OUTCOMES,
    _avatar,
    _build_board,
    _card_model,
    _check_credentials,
    _demo_managers,
    _initials,
    _now,
    _qualification_rows,
    current_manager,
    require_admin,
)
from app.config import settings
from app.core.branding import quick_replies_for
from app.core.leadstate import STAGE_TO_COLUMN
from app.integrations.panel.store import get_conversation_store

router = APIRouter(prefix="/admin-v2", tags=["admin-v2"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates" / "v2"))


# ============================================================================
# dialog_owner — canonical-preferred with a documented legacy compat shim (owner §1).
# ============================================================================
def _dialog_owner(conv, demo_paused_id: str | None) -> str:
    """Canonical `dialog_owner`: 'bot' | 'manager' | 'paused'.

    Preference order:
      1) A REAL `conv.dialog_owner` attribute, if the store ever provides one (the
         future canonical field on PilotConversation — not yet wired anywhere; this
         is forward-compatible so templates need NO change when it lands).
      2) LEGACY COMPAT SHIM (temporary, documented): `ConversationView` today only has
         `intercepted: bool` + `assigned_to: str` — no real "paused" concept.
         intercepted == False -> 'bot'; intercepted == True -> 'manager'.
         We deliberately do NOT infer 'paused' from "manager + no assigned_to" — the
         owner flagged that heuristic as WRONG (assigned_to names the manager but does
         not define the mode). 'paused' is ONLY ever shown via (1) or (3) below.
      3) DEMO-ONLY (prototype): to let the owner preview the 'paused' pill design
         before a real dialog_owner field exists, the caller may pass
         `demo_paused_id` — the user_id of exactly ONE conversation, chosen
         deterministically (see `_demo_paused_user_id` below) — which is shown as
         'paused' here in the VIEW MODEL ONLY. No store write, no other route/template
         is affected. Delete this branch once the backend exposes real dialog_owner.
    """
    real = getattr(conv, "dialog_owner", None)
    if real in ("bot", "manager", "paused"):
        return real
    if demo_paused_id is not None and conv.user_id == demo_paused_id:
        return "paused"
    return "manager" if conv.intercepted else "bot"


async def _demo_paused_user_id() -> str | None:
    """DEMO ONLY (Increment 8A prototype) — deterministically picks ONE conversation
    (lexicographically smallest user_id currently in the store, if any) to display as
    dialog_owner='paused', purely so the owner can see the paused-pill design. Stable
    across requests/tests (not random). Returns None if the store is empty. Remove this
    whole function + its call sites once the backend exposes a real dialog_owner."""
    convs = await get_conversation_store().all_conversations()
    if not convs:
        return None
    return min(c.user_id for c in convs)


def _with_owner(card: dict, conv, demo_paused_id: str | None) -> dict:
    """Card dict (from app.admin.router._card_model) + dialog_owner. Non-mutating."""
    card = dict(card)
    card["dialog_owner"] = _dialog_owner(conv, demo_paused_id)
    return card


# ============================================================================
# auth (same session cookie as /admin — current_manager/require_admin reused as-is)
# ============================================================================
@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if current_manager(request):
        return RedirectResponse("/admin-v2", status_code=303)
    return templates.TemplateResponse(request, "login.html",
                                      {"error": None, "demo_managers": _demo_managers()},
                                      headers={"Cache-Control": "no-store"})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, login: str = Form(...), password: str = Form(...)):
    from app.admin import ratelimit
    ip = request.client.host if request.client else "unknown"
    if ratelimit.is_blocked(ip):
        return templates.TemplateResponse(request, "login.html",
                                          {"error": "Слишком много попыток. Подождите минуту.",
                                           "demo_managers": _demo_managers()}, status_code=429)
    manager = _check_credentials(login.strip(), password)
    if manager is None:
        ratelimit.note_failure(ip)
        return templates.TemplateResponse(request, "login.html",
                                          {"error": "Неверный логин или пароль",
                                           "demo_managers": _demo_managers()}, status_code=401)
    request.session["manager"] = manager
    await get_conversation_store().add_audit(manager["login"], "login")
    return RedirectResponse("/admin-v2", status_code=303)


@router.post("/login/demo")
async def login_demo(request: Request, login: str = Form(...)):
    """Быстрый вход для демо — как в /admin/login/demo, доступен только при demo_login."""
    if not settings.demo_login:
        raise HTTPException(status_code=404, detail="not found")
    mgr = next((m for m in settings.manager_list() if m.login == login), None)
    if mgr is None:
        raise HTTPException(status_code=404, detail="manager not found")
    request.session["manager"] = {"login": mgr.login, "name": mgr.name or mgr.login}
    await get_conversation_store().add_audit(mgr.login, "login")
    return RedirectResponse("/admin-v2", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.pop("manager", None)
    return RedirectResponse("/admin-v2/login", status_code=303)


# ============================================================================
# shell
# ============================================================================
@router.get("", response_class=HTMLResponse)
async def index(request: Request):
    manager = current_manager(request)
    if not manager:
        return RedirectResponse("/admin-v2/login", status_code=303)
    return templates.TemplateResponse(request, "shell.html",
                                      {"manager": manager, "funnels": FUNNELS},
                                      headers={"Cache-Control": "no-store"})


# ============================================================================
# dialog list (vertical rows) — inbox / search / archive (owner §4 "Архив" filter)
# ============================================================================
async def _all_models_v2(now: datetime) -> tuple[list[dict], str | None]:
    convs = await get_conversation_store().all_conversations()
    demo_paused_id = await _demo_paused_user_id()
    return [_with_owner(_card_model(c, now), c, demo_paused_id) for c in convs], demo_paused_id


def _waiting_sorted(models: list[dict]) -> list[dict]:
    cards = [m for m in models if m["wait_level"] != "none"]
    cards.sort(key=lambda m: m["sort_key"], reverse=True)
    return cards


@router.get("/inbox", response_class=HTMLResponse)
async def inbox(request: Request, _: dict = Depends(require_admin)):
    models, _demo = await _all_models_v2(_now())
    cards = _waiting_sorted(models)
    return templates.TemplateResponse(request, "_dialog_list.html", {
        "mode": "inbox", "cards": cards, "query": "",
        "noise_count": sum(1 for c in cards if c["is_noise"]),
    })


@router.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", _: dict = Depends(require_admin)):
    q = q.strip()
    models, _demo = await _all_models_v2(_now())
    if not q:
        cards = _waiting_sorted(models)
        return templates.TemplateResponse(request, "_dialog_list.html", {
            "mode": "inbox", "cards": cards, "query": "",
            "noise_count": sum(1 for c in cards if c["is_noise"]),
        })
    ql = q.lower()
    cards = [m for m in models
             if ql in (m["name"] or "").lower() or ql in (m["phone"] or "").lower()
             or ql in (m["last_text"] or "").lower()]
    cards.sort(key=lambda m: m["sort_key"], reverse=True)
    return templates.TemplateResponse(request, "_dialog_list.html", {
        "mode": "search", "cards": cards, "query": q,
        "noise_count": sum(1 for c in cards if c["is_noise"]),
    })


async def _archived_cards(now: datetime, demo_paused_id: str | None) -> list[dict]:
    """Archived conversations for the 'Архив' section (owner §4 P0 fix).

    Reuses ONLY existing public store surface — store.py is not modified. There is
    no public "list archived" method (`all_conversations`/`list_cards` both filter
    archived rows out), so:
      * MemoryConversationStore (default backend; used by tests/dev): we read its
        `_conv` dict directly, READ-ONLY (never write) — the simplest correct option
        without adding a store method, which this increment is not allowed to do.
      * Any other backend (e.g. Postgres in prod): best-effort reconstruction from the
        audit log (`list_audit`, already public on both backends) — only catches
        SINGLE archive actions, since bulk archive/archive-noise audit rows don't
        record a per-conversation user_id (see app/admin/router.py's
        `add_audit(..., "archive_many", "", f"count={count}")`). Documented limitation
        for this prototype; a real "list archived" store method should replace this
        when /admin-v2 becomes the real admin UI.
    """
    store = get_conversation_store()
    conv_map = getattr(store, "_conv", None)
    if isinstance(conv_map, dict):
        convs = [c for c in conv_map.values() if c.archived]
        return [_with_owner(_card_model(c, now), c, demo_paused_id) for c in convs]
    seen: dict[str, str] = {}
    for row in await store.list_audit(500):
        uid = row.get("user_id") or ""
        if not uid or uid in seen:
            continue
        seen[uid] = row.get("action", "")
    cards = []
    for uid, action in seen.items():
        if action != "archive":
            continue
        conv = await store.get(uid)
        if conv is not None and conv.archived:
            cards.append(_with_owner(_card_model(conv, now), conv, demo_paused_id))
    return cards


@router.get("/archive", response_class=HTMLResponse)
async def archive_list(request: Request, _: dict = Depends(require_admin)):
    now = _now()
    demo_paused_id = await _demo_paused_user_id()
    cards = await _archived_cards(now, demo_paused_id)
    cards.sort(key=lambda m: m["sort_key"], reverse=True)
    return templates.TemplateResponse(request, "_dialog_list.html", {
        "mode": "archive", "cards": cards, "query": "", "noise_count": 0,
    })


@router.get("/stats", response_class=JSONResponse)
async def stats(_: dict = Depends(require_admin)):
    models, _demo = await _all_models_v2(_now())
    return JSONResponse({
        "waiting": sum(1 for m in models if m["wait_level"] != "none"),
        "needs_reply": sum(1 for m in models if m["needs_reply"]),
        "noise": sum(1 for m in models if m["is_noise"]),
        "total": len(models),
    })


# ============================================================================
# open dialog: chat (middle) + right lead panel (owner §6 hierarchy)
# ============================================================================
@router.get("/conversation/{user_id}", response_class=HTMLResponse)
async def conversation(user_id: str, request: Request, manager: dict = Depends(require_admin)):
    panel = get_conversation_store()
    conv = await panel.get(user_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    name = conv.qualification.get("name")
    conv.phone = conv.phone or conv.user_id
    busy_by = conv.assigned_to if conv.assigned_to and conv.assigned_to != manager["login"] else ""
    demo_paused_id = await _demo_paused_user_id()
    return templates.TemplateResponse(request, "_workspace.html", {
        "c": conv,
        "dialog_owner": _dialog_owner(conv, demo_paused_id),
        "initials": _initials(name, conv.phone),
        "avatar": _avatar(conv.phone),
        "manager": manager,
        "busy_by": busy_by,
        "outcomes": OUTCOMES,
        "quick_replies": quick_replies_for(conv.funnel),
        "qualification_rows": _qualification_rows(conv.qualification),
        "stage_label": dict(BOARD_COLUMNS).get(STAGE_TO_COLUMN.get(conv.stage, ""), conv.stage),
    })


# ============================================================================
# legacy kanban (owner §2) — same 7 BOARD_COLUMNS, unchanged, labeled temporary
# ============================================================================
@router.get("/board/{funnel}", response_class=HTMLResponse)
async def board(funnel: str, request: Request, _: dict = Depends(require_admin)):
    panel = get_conversation_store()
    cards = await panel.list_cards(funnel)
    now = _now()
    demo_paused_id = await _demo_paused_user_id()
    columns, metrics = _build_board(cards, now)
    conv_by_id = {c.user_id: c for c in cards}
    for col in columns:
        col["cards"] = [
            _with_owner(card, conv_by_id[card["user_id"]], demo_paused_id)
            for card in col["cards"]
        ]
    return templates.TemplateResponse(request, "_kanban.html", {
        "funnel": funnel, "columns": columns, "metrics": metrics,
    })

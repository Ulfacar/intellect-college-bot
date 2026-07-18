"""Роутер админ-панели (FastAPI + Jinja2 + HTMX).

Канбан-доски диалогов, полный контекст переписки, перехват (бот замолкает),
ответ менеджера клиенту, исход сделки. Аккаунты менеджеров — сессия (cookie),
список логинов в settings.managers. Действия пишутся в аудит-лог.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse)
from fastapi.templating import Jinja2Templates

from app.agent.llm import chat, llm_enabled
from app.channels import outbound
from app.config import settings
from app.core import telegram_sessions
from app.core.branding import quick_replies_for
from app.core.leadstate import HUMAN_STAGES, STAGE_TO_COLUMN, is_noise, is_silent
from app.core.state import get_state_store
from app.integrations.panel.store import get_conversation_store

log = logging.getLogger("admin")

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Доска приёмной комиссии.
FUNNELS = [("admission", "Абитуриенты")]
FUNNEL_LABELS = {"admission": "Приёмная"}
FAQ_TABS = FUNNELS + [("common", "Общие")]

# Колонки канбана и маппинг внутренних стадий диалога в колонку.
BOARD_COLUMNS = [
    ("greeting", "Новые лиды"),
    ("qualification", "Квалификация"),
    ("progress", "Консультация"),
    ("office", "Приглашён на тест"),
    ("manager", "У менеджера"),
    ("silent", "Молчат (на дожим)"),
    ("follow_up", "Повторное касание"),
]
# Обратный маппинг для ручного переноса (drag-and-drop): колонка → каноническая стадия.
# Стадии-ключи = ключи колонок, чтобы карточка осталась в той колонке, куда её положили.
COLUMN_TO_STAGE = {key: key for key, _ in BOARD_COLUMNS if key != "silent"}

# Палитра градиентов для аватаров (детерминированно по имени/номеру).
AVATAR_GRADIENTS = [
    "linear-gradient(135deg,#2dd4bf,#0d9488)",
    "linear-gradient(135deg,#818cf8,#4f46e5)",
    "linear-gradient(135deg,#c084fc,#7c3aed)",
    "linear-gradient(135deg,#fbbf24,#d97706)",
    "linear-gradient(135deg,#fb7185,#e11d48)",
    "linear-gradient(135deg,#38bdf8,#0284c7)",
    "linear-gradient(135deg,#34d399,#059669)",
]
WAIT_WARM_MIN = 5    # клиент ждёт дольше — карточка теплеет
WAIT_HOT_MIN = 20    # ждёт долго — горит

# Исходы диалога для ручной отметки менеджером.
OUTCOMES = [("won", "🎓 Поступает"), ("office", "📝 Пришёл на тест"), ("lost", "❌ Слив")]
QUALIFICATION_LABELS = {
    "name": "Имя",
    "grade_base": "База (после класса)",
    "direction": "Направление",
    "visit_time": "Удобное время (тест)",
    "escalation_reason": "Причина эскалации",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _minutes_since(dt: datetime | None, now: datetime) -> float | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # SQLite отдаёт naive — считаем UTC
    return max(0.0, (now - dt).total_seconds() / 60)


def _initials(name: str | None, user_id: str) -> str:
    if name:
        parts = name.split()
        if len(parts) >= 2:
            return (parts[0][:1] + parts[1][:1]).upper()
        return name[:2].upper()
    return user_id[-2:]


def _avatar(user_id: str) -> str:
    return AVATAR_GRADIENTS[sum(user_id.encode()) % len(AVATAR_GRADIENTS)]


def _time_label(mins: float | None) -> str:
    if mins is None:
        return ""
    if mins < 1:
        return "сейчас"
    if mins < 60:
        return f"{int(mins)} мин"
    if mins < 1440:
        return f"{int(mins // 60)} ч"
    return f"{int(mins // 1440)} дн"


def _card_model(conv, now: datetime) -> dict:
    """Обогащённая карточка для доски: аватар, сигналы срочности, время, «кто ведёт»."""
    name = conv.qualification.get("name")
    phone = conv.phone or conv.user_id
    since = _minutes_since(conv.last_message_at, now)
    # «Клиент ждёт» = последним писал клиент и ему ещё не ответили (ни бот, ни менеджер).
    waiting = conv.last_sender == "client"
    wait_min = since if waiting else None
    if wait_min is None:
        level = "none"
    elif wait_min >= WAIT_HOT_MIN:
        level = "hot"
    elif wait_min >= WAIT_WARM_MIN:
        level = "warm"
    else:
        level = "fresh"
    # «Требуют ответа человека» = клиент ждёт И диалог у менеджера/перехвачен.
    needs_reply = waiting and (conv.intercepted or conv.stage in HUMAN_STAGES)
    noise = is_noise(conv, now, settings)
    silent = is_silent(conv, now, settings)
    return {
        "user_id": conv.user_id, "phone": phone, "name": name or phone,
        "initials": _initials(name, phone),
        "avatar": _avatar(phone),
        "channel": conv.channel, "stage": conv.stage, "intercepted": conv.intercepted,
        "funnel": conv.funnel or "", "funnel_label": FUNNEL_LABELS.get(conv.funnel, conv.funnel or "—"),
        "assigned_to": conv.assigned_to, "outcome": conv.outcome,
        "last_text": conv.last_text, "last_sender": conv.last_sender,
        "time_label": _time_label(since),
        "wait_label": _time_label(wait_min) if wait_min is not None else "",
        "wait_level": level,                       # none|fresh|warm|hot
        "needs_reply": needs_reply,
        "is_noise": noise,
        "is_silent": silent,
        "lead_temperature": conv.lead_temperature,
        "qualification_line": _qualification_line(conv.qualification),
        "sort_key": (wait_min if wait_min is not None else -1),
    }


def _grade_label(value: str | None) -> str:
    if value == "9":
        return "после 9 класса"
    if value == "11":
        return "после 11 класса"
    return value or ""


def _qualification_line(q: dict | None) -> str:
    q = q or {}
    parts = [_grade_label(q.get("grade_base")), q.get("direction") or ""]
    return " · ".join(p for p in parts if p)


def _qualification_rows(q: dict | None) -> list[tuple[str, str]]:
    q = q or {}
    ordered = ["name", "grade_base", "direction", "visit_time", "escalation_reason"]
    keys = ordered + [k for k in q if k not in ordered]
    rows: list[tuple[str, str]] = []
    for key in keys:
        if key not in q:
            continue
        value = _grade_label(q.get(key)) if key == "grade_base" else str(q.get(key) or "")
        if value:
            rows.append((QUALIFICATION_LABELS.get(key, key), value))
    return rows


# ---------------- авторизация (сессия менеджера) ----------------
def current_manager(request: Request) -> dict | None:
    """Текущий менеджер из cookie-сессии (или None)."""
    m = request.session.get("manager")
    return m if isinstance(m, dict) else None


def require_admin(request: Request) -> dict:
    """Зависимость: пускаем только залогиненного менеджера, иначе 401."""
    m = current_manager(request)
    if not m:
        raise HTTPException(status_code=401, detail="login required")
    return m


def _check_credentials(login: str, password: str) -> dict | None:
    for mgr in settings.manager_list():
        if (secrets.compare_digest(login, mgr.login)
                and secrets.compare_digest(password, mgr.password)):
            return {"login": mgr.login, "name": mgr.name or mgr.login}
    return None


def _demo_managers() -> list[dict]:
    """Список менеджеров для кнопок быстрого входа (только при demo_login,
    force-disabled in production — Increment 8B §11, `settings.demo_login_available()`)."""
    if not settings.demo_login_available():
        return []
    return [{"login": m.login, "name": m.name or m.login} for m in settings.manager_list()]


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if current_manager(request):
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(request, "login.html",
                                      {"error": None, "demo_managers": _demo_managers()},
                                      headers={"Cache-Control": "no-store"})


@router.post("/login/demo")
async def login_demo(request: Request, login: str = Form(...)):
    """Быстрый вход для демо (без пароля). Доступен ТОЛЬКО при settings.demo_login_available()
    — Increment 8B §11: force-disabled when settings.environment == "production"."""
    if not settings.demo_login_available():
        raise HTTPException(status_code=404, detail="not found")
    mgr = next((m for m in settings.manager_list() if m.login == login), None)
    if mgr is None:
        raise HTTPException(status_code=404, detail="manager not found")
    request.session["manager"] = {"login": mgr.login, "name": mgr.name or mgr.login}
    await get_conversation_store().add_audit(mgr.login, "login")
    return RedirectResponse("/admin", status_code=303)


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
        ratelimit.note_failure(ip)        # к блокировке ведут только провалы
        return templates.TemplateResponse(request, "login.html",
                                          {"error": "Неверный логин или пароль",
                                           "demo_managers": _demo_managers()}, status_code=401)
    request.session["manager"] = manager
    await get_conversation_store().add_audit(manager["login"], "login")
    return RedirectResponse("/admin", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.pop("manager", None)
    return RedirectResponse("/admin/login", status_code=303)


def _build_board(cards: list, now: datetime) -> tuple[list[dict], dict]:
    """Колонки канбана (карточки обогащены и отсортированы: ждут дольше — наверх) + метрики."""
    buckets: dict[str, list] = {key: [] for key, _ in BOARD_COLUMNS}
    models = [_card_model(c, now) for c in cards]
    for m in models:
        column = "silent" if m["is_silent"] else STAGE_TO_COLUMN.get(m["stage"], "greeting")
        buckets[column].append(m)
    for col in buckets.values():
        col.sort(key=lambda m: m["sort_key"], reverse=True)  # горячие наверх
    columns = [{"key": key, "label": label, "cards": buckets[key], "is_empty": not buckets[key]}
               for key, label in BOARD_COLUMNS]
    metrics = {
        "total": len(cards),
        "waiting": sum(1 for m in models if m["wait_level"] != "none"),
        "needs_reply": sum(1 for m in models if m["needs_reply"]),
        "noise": sum(1 for m in models if m["is_noise"]),
        "silent": sum(1 for m in models if m["is_silent"]),
        "intercepted": sum(1 for c in cards if c.intercepted),
    }
    return columns, metrics


@router.get("", response_class=HTMLResponse)
async def index(request: Request):
    """Главная страница панели с вкладками-досками. Без сессии — на форму логина."""
    manager = current_manager(request)
    if not manager:
        return RedirectResponse("/admin/login", status_code=303)
    return templates.TemplateResponse(request, "boards.html",
                                      {"funnels": FUNNELS, "manager": manager},
                                      headers={"Cache-Control": "no-store"})


@router.get("/analytics", response_class=HTMLResponse)
async def analytics(request: Request, period: str = "all",
                    manager: dict = Depends(require_admin)):
    """Дашборд «ИИ vs менеджер»: containment, исходы, воронки, время ответа/перехвата.
    period — окно периода (today|7d|30d|all)."""
    from app.integrations.panel.analytics import PERIODS, compute_analytics
    convs = await get_conversation_store().all_conversations()
    data = compute_analytics(convs, period=period, now=_now())
    return templates.TemplateResponse(request, "analytics.html",
                                      {"a": data, "manager": manager, "funnels": FUNNELS,
                                       "periods": PERIODS, "period": period},
                                      headers={"Cache-Control": "no-store"})


@router.get("/system", response_class=HTMLResponse)
async def system(request: Request, manager: dict = Depends(require_admin)):
    """Статус системы: LLM, тишина вебхуков, бэкенды, счётчики сбоев, боты."""
    from app.core import observ
    snap = observ.snapshot()
    flag_views = await _flag_views()
    bot_flags = await _bot_flag_views()
    data = {
        "llm_enabled": llm_enabled(),
        "last_inbound_ago": observ.last_inbound_ago(),
        "state_backend": settings.state_backend,
        "panel_backend": settings.panel_backend,
        "crm_backend": settings.crm_backend,
        "followup_enabled": settings.followup_enabled,
        "alerts_configured": bool(settings.alert_whatsapp_to and settings.alert_bot_id),
        "webhook_secret_set": bool(settings.webhook_secret),
        "llm_failures": snap.get("llm_failures", 0),
        "send_failures": snap.get("send_failures", 0),
        "llm_failure_ago": snap.get("llm_failure_ago"),
        "send_failure_ago": snap.get("send_failure_ago"),
    }
    return templates.TemplateResponse(request, "system.html",
                                      {"s": data, "manager": manager,
                                       "flags": flag_views, "bot_flags": bot_flags},
                                      headers={"Cache-Control": "no-store"})


# Тумблеры фич для менеджера: ключ → заголовок, описание, дефолт (из env), примечание.
FEATURE_FLAGS = {
    "bots_enabled": {
        "title": "Авто-ответы бота (главный рубильник)",
        "desc": ("Если выключить — бот перестаёт отвечать клиентам во всех воронках "
                 "приёмной. Входящие сообщения по-прежнему попадают в панель, "
                 "и менеджеры ведут диалоги вручную. Включите обратно, чтобы бот снова "
                 "отвечал автоматически."),
        "default": lambda: True,
        "note": lambda: "",
    },
    "followup_enabled": {
        "title": "Автодожим молчащих клиентов",
        "desc": ("Если клиент замолчал на этапе квалификации дольше 24 часов, бот сам отправит "
                 "один мягкий напоминающий месседж и переместит карточку в «Повторное касание». "
                 "Ночью (22:00–09:00 по Бишкеку) не беспокоит. Каждому клиенту — не больше одного "
                 "раза; как только клиент ответит, диалог продолжается обычным образом."),
        "default": lambda: settings.followup_enabled,
        "note": lambda: "",
    },
    "alerts_enabled": {
        "title": "Watchdog-алерты",
        "desc": ("Уведомлять администратора в WhatsApp, если бот не получает входящих дольше "
                 "30 минут или пошёл всплеск сбоев (LLM/отправка). Помогает заметить, что бот "
                 "«отвалился», раньше, чем начнут жаловаться клиенты."),
        "default": lambda: True,
        "note": lambda: ("" if (settings.alert_whatsapp_to and settings.alert_bot_id)
                         else "⚠️ Чтобы алерты отправлялись, задайте в prod.env номер админа "
                              "(ALERT_WHATSAPP_TO) и бота (ALERT_BOT_ID)."),
    },
}


async def _flag_views() -> list[dict]:
    """Состояние всех тумблеров для рендера (значение из БД, дефолт из env)."""
    from app.core import flags
    views = []
    for key, spec in FEATURE_FLAGS.items():
        on = await flags.get_flag(key, spec["default"]())
        views.append({"key": key, "title": spec["title"], "desc": spec["desc"],
                      "on": on, "note": spec["note"]()})
    return views


SCENARIO_LABELS = {"admission": "Приёмная"}


async def _bot_flag_views() -> list[dict]:
    """Эффективное состояние per-bot тумблеров с наследованием от главного рубильника."""
    from app.core import flags
    from app.core.bots import registry
    global_on = await flags.get_flag("bots_enabled", True)
    views = []
    for bot in registry.all():
        on = await flags.get_flag(f"bots_enabled:{bot.id}", global_on)
        channel = "WhatsApp" if bot.wappi_profile_id else "Telegram"
        profile = bot.wappi_profile_id or bot.bitrix_bot_id or bot.bitrix_line_id or ""
        views.append({
            "id": bot.id,
            "key": f"bots_enabled:{bot.id}",
            "title": bot.title or bot.id,
            "scenario": bot.scenario,
            "scenario_label": SCENARIO_LABELS.get(bot.scenario, bot.scenario),
            "channel": channel,
            "profile": profile,
            "on": on,
        })
    return views


@router.post("/flags/{key}", response_class=HTMLResponse)
async def toggle_flag(key: str, request: Request, manager: dict = Depends(require_admin),
                      on: str = Form("0")):
    """Менеджер включает/выключает фичу кнопкой в панели (рантайм-флаг в БД, без рестарта)."""
    if key not in FEATURE_FLAGS:
        raise HTTPException(status_code=404, detail="unknown flag")
    from app.core import flags
    value = on in ("1", "true", "on", "True")
    await flags.set_flag(key, value)
    await get_conversation_store().add_audit(
        manager["login"], "flag", "", f"{key}={'on' if value else 'off'}")
    return templates.TemplateResponse(request, "_automation.html", {"flags": await _flag_views()})


@router.post("/bots/{bot_id}/toggle", response_class=HTMLResponse)
async def toggle_bot_flag(bot_id: str, request: Request, manager: dict = Depends(require_admin),
                          on: str = Form("0")):
    """Менеджер включает/выключает авто-ответы конкретного бота."""
    from app.core import flags
    from app.core.bots import registry
    bot = registry.by_id(bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail="unknown bot")
    value = on in ("1", "true", "on", "True")
    key = f"bots_enabled:{bot_id}"
    await flags.set_flag(key, value)
    await get_conversation_store().add_audit(
        manager["login"], "flag", "", f"{key}={'on' if value else 'off'}")
    return templates.TemplateResponse(request, "_bot_toggles.html",
                                      {"bot_flags": await _bot_flag_views()})


@router.get("/audit", response_class=HTMLResponse)
async def audit(request: Request, manager: dict = Depends(require_admin)):
    """Журнал действий менеджеров (перехват/ответ/исход/перенос/логин)."""
    rows = await get_conversation_store().list_audit(200)
    return templates.TemplateResponse(request, "audit.html",
                                      {"rows": rows, "manager": manager},
                                      headers={"Cache-Control": "no-store"})


def _lines(raw: str) -> list[str]:
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def _faq_scope(scope: str) -> str | None:
    return scope if scope in FUNNEL_LABELS else None


@router.get("/faq", response_class=HTMLResponse)
async def faq_page(request: Request, scope: str = "admission",
                   manager: dict = Depends(require_admin)):
    """Редактор FAQ-правил: детерминированные ответы до LLM."""
    from app.core.faq import get_faq_store
    scope = scope if scope in {"admission", "common"} else "admission"
    store = get_faq_store()
    rows = await store.list(scope)
    edit_id = int(request.query_params.get("edit") or 0)
    edit = await store.get(edit_id) if edit_id else None
    return templates.TemplateResponse(request, "faq.html", {
        "manager": manager, "tabs": FAQ_TABS, "scope": scope, "rows": rows,
        "edit": edit, "funnels": FUNNELS,
    }, headers={"Cache-Control": "no-store"})


@router.post("/faq/save", response_class=HTMLResponse)
async def faq_save(request: Request, manager: dict = Depends(require_admin),
                   entry_id: int = Form(0), scope: str = Form("common"),
                   title: str = Form(""), patterns: str = Form(""),
                   negative_terms: str = Form(""), answer: str = Form(""),
                   priority: int = Form(0), enabled: str = Form("0"),
                   handoff_only: str = Form("0"),
                   allow_during_qualification: str = Form("0")):
    """Создать или обновить FAQ-правило."""
    from app.core.faq import get_faq_store
    data = {
        "id": entry_id,
        "funnel": _faq_scope(scope),
        "enabled": enabled in ("1", "true", "on", "True"),
        "priority": priority,
        "title": title,
        "patterns": _lines(patterns),
        "negative_terms": _lines(negative_terms),
        "answer": answer,
        "handoff_only": handoff_only in ("1", "true", "on", "True"),
        "allow_during_qualification": allow_during_qualification in ("1", "true", "on", "True"),
    }
    if not data["title"] or not data["patterns"] or not data["answer"]:
        raise HTTPException(status_code=400, detail="title, patterns and answer are required")
    row = await get_faq_store().upsert(data, manager["login"])
    action = "faq_update" if entry_id else "faq_create"
    await get_conversation_store().add_audit(manager["login"], action, "", f"{row.id}: {row.title}")
    return RedirectResponse(f"/admin/faq?scope={scope}", status_code=303)


@router.post("/faq/{entry_id}/toggle")
async def faq_toggle(entry_id: int, scope: str = Form("common"),
                     enabled: str = Form("0"), manager: dict = Depends(require_admin)):
    """Включить/выключить FAQ-правило."""
    from app.core.faq import get_faq_store
    value = enabled in ("1", "true", "on", "True")
    store = get_faq_store()
    row = await store.get(entry_id)
    await store.set_enabled(entry_id, value, manager["login"])
    await get_conversation_store().add_audit(
        manager["login"], "faq_update" if value else "faq_disable", "",
        f"{entry_id}: {(row.title if row else '')}"
    )
    return RedirectResponse(f"/admin/faq?scope={scope}", status_code=303)


@router.post("/faq/test", response_class=HTMLResponse)
async def faq_test(request: Request, manager: dict = Depends(require_admin),
                   scope: str = Form("common"), text: str = Form("")):
    """Проверить фразу через тот же матчинг, без отправки клиенту."""
    from app.core.faq import get_faq_store, match_faq
    scope = scope if scope in {"admission", "common"} else "common"
    funnel = _faq_scope(scope)
    store = get_faq_store()
    entries = await store.candidates(funnel)
    hit = match_faq(text, funnel, entries)
    return templates.TemplateResponse(request, "faq.html", {
        "manager": manager, "tabs": FAQ_TABS, "scope": scope,
        "rows": await store.list(scope), "edit": None, "funnels": FUNNELS,
        "test_text": text, "test_hit": hit, "tested": True,
    }, headers={"Cache-Control": "no-store"})


# ========================================================================================
# База знаний / FAQ (Increment 5) — managed multilingual FAQ, separate from the legacy
# section above (`faq_entries`/`app/core/faq.py`, untouched). See `app/core/faq_kb.py`
# (store + publication lifecycle) and `app/core/faq_matcher.py` (pure matching).
# ========================================================================================

FAQ_KB_CATEGORY_LABELS: dict[str, str] = {
    "general": "Общее",
    "contacts": "Контакты",
    "schedule": "Часы работы / расписание",
    "admission": "Поступление",
    "documents": "Документы",
    "directions": "Направления",
    "tuition": "Стоимость обучения",
    "discounts": "Скидки",
    "payment": "Оплата",
    "entrance_test": "Вступительный тест",
    "passing_score": "Проходной балл",
    "deadlines": "Сроки / дедлайны",
    "contract": "Договор",
    "infrastructure": "Инфраструктура",
    "employment": "Трудоустройство",
    "international": "Международное",
    "other": "Другое",
}

# RU labels (UI-only mapping — do NOT change store/matcher semantics). Raw enum codes
# stay canonical in the backend; these dicts humanize them in templates.
FAQ_KB_STATUS_LABELS: dict[str, str] = {
    "draft": "черновик", "published": "опубликовано", "archived": "архив",
}
# Confirm-banner lifecycle action codes (publish/disable/archive/enable/rollback).
FAQ_KB_LIFECYCLE_ACTION_LABELS: dict[str, str] = {
    "publish": "Опубликовать", "disable": "Выключить", "archive": "Удалить (архив)",
    "enable": "Включить", "rollback": "Откат",
}
# Version-history action codes.
FAQ_KB_VERSION_ACTION_LABELS: dict[str, str] = {
    "created": "создано", "edited": "изменено", "published": "опубликовано",
    "disabled": "выключено", "enabled": "включено", "archived": "архивировано",
    "restored": "откат к версии",
}
# Matcher match_type codes.
FAQ_KB_MATCH_TYPE_LABELS: dict[str, str] = {
    "canonical": "точный вопрос", "variant": "вариант вопроса",
    "normalized": "частичное совпадение", "fuzzy": "похожая формулировка",
}


def _truthy(v: str) -> bool:
    return v in ("1", "true", "on", "True")


def _tri_bool(v: str) -> bool | None:
    """Tri-state filter select: "" -> любое, "1" -> да, "0" -> нет."""
    if v == "1":
        return True
    if v == "0":
        return False
    return None


def _parse_dt_local(raw: str) -> datetime | None:
    """`<input type="datetime-local">` (`YYYY-MM-DDTHH:MM`) -> aware UTC datetime."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fmt_dt_local(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M")


def _parse_variant_lines(raw: str) -> list[dict]:
    """One structured variant per line. Optional `ru|`/`ky|` prefix pins the
    variant's language; no prefix -> language=None (matches either)."""
    out = []
    for line in _lines(raw):
        prefix = line[:3].lower()
        if prefix in ("ru|", "ky|"):
            out.append({"text": line[3:].strip(), "language": prefix[:2]})
        else:
            out.append({"text": line.strip(), "language": None})
    return [v for v in out if v["text"]]


def _variants_to_lines(variants) -> str:
    lines = []
    for v in variants:
        lines.append(f"{v.language}|{v.text}" if v.language else v.text)
    return "\n".join(lines)


async def _faq_kb_context(request: Request, manager: dict, *, edit_id: int = 0) -> dict:
    from app.core.faq_kb import CATEGORIES, SENSITIVE_CATEGORIES, get_faq_kb_store

    qp = request.query_params
    status = qp.get("status", "")
    category = qp.get("category", "")
    enabled = qp.get("enabled", "")
    missing_ky = qp.get("missing_ky", "")
    handoff_only = qp.get("handoff_only", "")
    expired = qp.get("expired", "")
    q = qp.get("q", "")

    store = get_faq_kb_store()
    rows = await store.list_entries(
        status=status or None, category=category or None, enabled=_tri_bool(enabled),
        missing_ky=_tri_bool(missing_ky), handoff_only=_tri_bool(handoff_only),
        expired=_tri_bool(expired), search=q or None,
    )
    edit_id = edit_id or int(qp.get("edit") or 0)
    edit = await store.get_entry(edit_id) if edit_id else None
    edit_variants = await store.list_variants(edit_id) if edit_id else []
    return {
        "manager": manager, "rows": rows, "categories": CATEGORIES,
        "category_labels": FAQ_KB_CATEGORY_LABELS, "sensitive_categories": SENSITIVE_CATEGORIES,
        "status_labels": FAQ_KB_STATUS_LABELS, "action_labels": FAQ_KB_LIFECYCLE_ACTION_LABELS,
        "match_type_labels": FAQ_KB_MATCH_TYPE_LABELS,
        "filters": {"status": status, "category": category, "enabled": enabled,
                    "missing_ky": missing_ky, "handoff_only": handoff_only,
                    "expired": expired, "q": q},
        "edit": edit, "edit_variants_text": _variants_to_lines(edit_variants),
        "confirm_required": qp.get("confirm_required", ""),
        "backfilled": qp.get("backfilled", ""),
        "playground": None,
    }


@router.get("/faq-kb", response_class=HTMLResponse)
async def faq_kb_page(request: Request, manager: dict = Depends(require_admin)):
    """Раздел «База знаний / FAQ» — управляемая мультиязычная база (Increment 5)."""
    ctx = await _faq_kb_context(request, manager)
    return templates.TemplateResponse(request, "faq_kb.html", ctx, headers={"Cache-Control": "no-store"})


@router.post("/faq-kb/save", response_class=HTMLResponse)
async def faq_kb_save(request: Request, manager: dict = Depends(require_admin),
                      entry_id: int = Form(0), canonical_question: str = Form(""),
                      variants: str = Form(""), answer_ru: str = Form(""),
                      answer_ky: str = Form(""), category: str = Form("general"),
                      priority: int = Form(0), handoff_only: str = Form("0"),
                      valid_from: str = Form(""), valid_until: str = Form("")):
    """«Сохранить черновик» — создать/обновить контент. НЕ публикует (бот не увидит
    изменения, пока не нажать «Опубликовать» — см. app/core/faq_kb.py)."""
    from app.core.faq_kb import get_faq_kb_store

    if not canonical_question.strip() or not answer_ru.strip():
        raise HTTPException(status_code=400, detail="canonical_question and answer_ru are required")
    data = {
        "canonical_question": canonical_question, "answer_ru": answer_ru,
        "answer_ky": answer_ky or None, "category": category, "priority": priority,
        "handoff_only": _truthy(handoff_only),
        "valid_from": _parse_dt_local(valid_from), "valid_until": _parse_dt_local(valid_until),
    }
    variant_rows = _parse_variant_lines(variants)
    store = get_faq_kb_store()
    if entry_id:
        entry = await store.update_draft(entry_id, data, variant_rows, manager["login"])
        action = "faq_kb_update"
    else:
        entry = await store.create_draft(data, variant_rows, manager["login"])
        action = "faq_kb_create"
    if entry is None:
        raise HTTPException(status_code=404, detail="faq-kb entry not found")
    await get_conversation_store().add_audit(manager["login"], action, "", f"{entry.id}: {entry.canonical_question}")
    return RedirectResponse(f"/admin/faq-kb?edit={entry.id}", status_code=303)


async def _faq_kb_lifecycle_action(entry_id: int, manager: dict, action: str, *, confirm: str, **kwargs) -> RedirectResponse:
    from app.core.faq_kb import get_faq_kb_store

    store = get_faq_kb_store()
    method = getattr(store, action)
    result = await method(entry_id, manager["login"], confirm=_truthy(confirm), **kwargs)
    if not result.ok and result.error == "confirmation_required":
        return RedirectResponse(f"/admin/faq-kb?edit={entry_id}&confirm_required={action}", status_code=303)
    if result.ok:
        await get_conversation_store().add_audit(manager["login"], f"faq_kb_{action}", "", str(entry_id))
    return RedirectResponse(f"/admin/faq-kb?edit={entry_id}", status_code=303)


@router.post("/faq-kb/{entry_id}/publish")
async def faq_kb_publish(entry_id: int, manager: dict = Depends(require_admin), confirm: str = Form("0")):
    """Публикация. Чувствительные категории (§5) требуют явного подтверждения —
    иначе остаётся черновиком/текущим статусом."""
    return await _faq_kb_lifecycle_action(entry_id, manager, "publish", confirm=confirm)


@router.post("/faq-kb/{entry_id}/disable")
async def faq_kb_disable(entry_id: int, manager: dict = Depends(require_admin), confirm: str = Form("0")):
    """Выключить (enabled=false) — немедленно перестаёт отвечать. Отключение УЖЕ
    опубликованного правила требует подтверждения."""
    return await _faq_kb_lifecycle_action(entry_id, manager, "disable", confirm=confirm)


@router.post("/faq-kb/{entry_id}/enable")
async def faq_kb_enable(entry_id: int, manager: dict = Depends(require_admin)):
    from app.core.faq_kb import get_faq_kb_store

    store = get_faq_kb_store()
    result = await store.enable(entry_id, manager["login"])
    if result.ok:
        await get_conversation_store().add_audit(manager["login"], "faq_kb_enable", "", str(entry_id))
    return RedirectResponse(f"/admin/faq-kb?edit={entry_id}", status_code=303)


@router.post("/faq-kb/{entry_id}/archive")
async def faq_kb_archive(entry_id: int, manager: dict = Depends(require_admin), confirm: str = Form("0")):
    """«Удалить» = архивировать (soft-delete). Хард-delete запрещён — история хранится
    всегда. Требует подтверждения без исключений."""
    return await _faq_kb_lifecycle_action(entry_id, manager, "archive", confirm=confirm)


@router.get("/faq-kb/{entry_id}/versions", response_class=HTMLResponse)
async def faq_kb_versions_page(entry_id: int, request: Request, manager: dict = Depends(require_admin)):
    """История версий + форма отката."""
    from app.core.faq_kb import get_faq_kb_store

    store = get_faq_kb_store()
    entry = await store.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="faq-kb entry not found")
    versions = list(reversed(await store.list_versions(entry_id)))
    return templates.TemplateResponse(request, "faq_kb_versions.html", {
        "manager": manager, "entry": entry, "versions": versions,
        "category_labels": FAQ_KB_CATEGORY_LABELS,
        "version_action_labels": FAQ_KB_VERSION_ACTION_LABELS,
        "confirm_required": request.query_params.get("confirm_required", ""),
    }, headers={"Cache-Control": "no-store"})


@router.post("/faq-kb/{entry_id}/rollback")
async def faq_kb_rollback(entry_id: int, manager: dict = Depends(require_admin),
                          version_number: int = Form(...), confirm: str = Form("0")):
    """«Откатить к версии N» — создаёт НОВУЮ версию (action=restored) из старого
    снапшота; история не удаляется. Требует подтверждения без исключений."""
    from app.core.faq_kb import get_faq_kb_store

    store = get_faq_kb_store()
    result = await store.rollback(entry_id, version_number, manager["login"], confirm=_truthy(confirm))
    if not result.ok and result.error == "confirmation_required":
        return RedirectResponse(f"/admin/faq-kb/{entry_id}/versions?confirm_required=1", status_code=303)
    if result.ok:
        await get_conversation_store().add_audit(
            manager["login"], "faq_kb_rollback", "", f"{entry_id} -> v{version_number}")
    return RedirectResponse(f"/admin/faq-kb?edit={entry_id}", status_code=303)


@router.post("/faq-kb/playground", response_class=HTMLResponse)
async def faq_kb_playground(request: Request, manager: dict = Depends(require_admin),
                            mode: str = Form("published"), entry_id: int = Form(0),
                            text: str = Form(""), language: str = Form("auto")):
    """Тест-панель: (1) Published only — реальное поведение бота; (2) Preview draft —
    проверка редактируемого черновика ДО публикации (никогда не попадает в реальный
    Telegram-конвейер — просто отдельный вызов матчера)."""
    from app.core import faq_kb, faq_matcher

    store = faq_kb.get_faq_kb_store()
    if mode == "draft" and entry_id:
        candidate = await store.get_entry_candidate(entry_id)
        candidates = [candidate] if candidate else []
    else:
        mode = "published"
        candidates = await store.list_published_candidates()

    lang = language if language in ("ru", "ky") else faq_matcher.detect_language(text)
    result = None
    if text.strip():
        result = faq_matcher.match(text, candidates, language=lang)

    ctx = await _faq_kb_context(request, manager, edit_id=entry_id)
    ctx["playground"] = {
        "mode": mode, "entry_id": entry_id, "text": text, "language": language, "result": result,
    }
    return templates.TemplateResponse(request, "faq_kb.html", ctx, headers={"Cache-Control": "no-store"})


@router.post("/faq-kb/backfill")
async def faq_kb_backfill(manager: dict = Depends(require_admin)):
    """Идемпотентный импорт legacy `faq_entries` как черновиков (никогда не
    авто-публикует). Повторный вызов не дублирует уже импортированные строки."""
    from app.core.faq_kb import get_faq_kb_store

    count = await get_faq_kb_store().backfill_legacy(manager["login"])
    await get_conversation_store().add_audit(manager["login"], "faq_kb_backfill", "", f"imported={count}")
    return RedirectResponse(f"/admin/faq-kb?backfilled={count}", status_code=303)


@router.get("/board/{funnel}", response_class=HTMLResponse)
async def board(funnel: str, request: Request, _: dict = Depends(require_admin)):
    """HTMX-партиал одной доски: колонки по стадиям с карточками."""
    panel = get_conversation_store()
    cards = await panel.list_cards(funnel)
    columns, metrics = _build_board(cards, _now())
    return templates.TemplateResponse(request, "_board.html", {
        "funnel": funnel, "columns": columns, "metrics": metrics,
    })


async def _all_models(now: datetime) -> list[dict]:
    """Обогащённые карточки по ВСЕМ воронкам (для инбокса, поиска, счётчиков)."""
    convs = await get_conversation_store().all_conversations()
    return [_card_model(c, now) for c in convs]


def _waiting_sorted(models: list[dict]) -> list[dict]:
    """Кто ждёт ответа (последним писал клиент), дольше всех — наверх."""
    cards = [m for m in models if m["wait_level"] != "none"]
    cards.sort(key=lambda m: m["sort_key"], reverse=True)
    return cards


async def _render_inbox_partial(request: Request, *, mode: str = "inbox", query: str = ""):
    models = await _all_models(_now())
    query = query.strip()
    if mode == "search" and query:
        ql = query.lower()
        cards = [m for m in models
                 if ql in (m["name"] or "").lower() or ql in (m["phone"] or "").lower()
                 or ql in (m["last_text"] or "").lower()]
        cards.sort(key=lambda m: m["sort_key"], reverse=True)
    else:
        cards = _waiting_sorted(models)
        mode = "inbox"
        query = ""
    return templates.TemplateResponse(request, "_attention.html",
                                      {"mode": mode, "cards": cards, "query": query,
                                       "noise_count": sum(1 for c in cards if c["is_noise"])})


@router.get("/inbox", response_class=HTMLResponse)
async def inbox(request: Request, _: dict = Depends(require_admin)):
    """Единый инбокс: все ждущие ответа диалоги по всем воронкам в одном списке."""
    return await _render_inbox_partial(request)


@router.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", _: dict = Depends(require_admin)):
    """Поиск по имени / номеру / последнему сообщению across все воронки.
    Пустой запрос возвращает инбокс — так очистка поля возвращает менеджера к списку."""
    q = q.strip()
    if not q:
        return await _render_inbox_partial(request)
    return await _render_inbox_partial(request, mode="search", query=q)


@router.get("/stats", response_class=JSONResponse)
async def stats(_: dict = Depends(require_admin)):
    """Лёгкий счётчик для звуковых уведомлений и бейджа в заголовке вкладки."""
    models = await _all_models(_now())
    return JSONResponse({
        "waiting": sum(1 for m in models if m["wait_level"] != "none"),
        "needs_reply": sum(1 for m in models if m["needs_reply"]),
        "noise": sum(1 for m in models if m["is_noise"]),
        "total": len(models),
    })


def _split_bot_user(user_id: str) -> tuple[str, str]:
    """Panel-store user_id is "<bot_id>:<external_user_id>" (see
    telegram_commands._log_to_legacy_panel). Dev-demo / non-pilot keys may be a bare
    number with no bot_id — those have no Telegram-pilot session at all."""
    bot_id, sep, ext = user_id.partition(":")
    return (bot_id, ext) if sep else ("", user_id)


def _is_pilot_conversation(user_id: str, conv) -> bool:
    """Increment 8B §8 (owner fail-closed rule): a Telegram-pilot conversation is
    session-aware — its user_id carries a "<bot_id>:<external_user_id>" AND it flows over
    the telegram channel, so an active pilot session MUST be verifiable. Such a
    conversation fails CLOSED when the session cannot be checked. Everything else
    (dev-demo keys without a bot_id, WhatsApp/Bitrix legacy conversations) objectively has
    no session model and stays fail-OPEN for backward compatibility."""
    bot_id, ext = _split_bot_user(user_id)
    return bool(bot_id and ext and (getattr(conv, "channel", "") or "") == "telegram")


async def _active_session_id(user_id: str) -> str | None:
    """Increment 8B §8: current ACTIVE Telegram-pilot session id for this panel
    conversation. Three distinct outcomes the callers rely on:
      * ""   — there is definitively NO pilot session (dev-demo / non-pilot key);
      * None — the lookup could not be performed (backend error) — callers fail CLOSED for
               a pilot conversation and OPEN for a legacy one (see _assert_manual_reply_allowed);
      * else — the session_id of the one active session.
    Read-only. Render sites coerce None/""→"" (an outage must never break rendering)."""
    bot_id, ext = _split_bot_user(user_id)
    if not (bot_id and ext):
        return ""
    try:
        active, _ = await telegram_sessions.get_active_session(bot_id, ext)
    except Exception:  # noqa: BLE001 — surface as None so the guard can fail closed
        log.warning("active-session lookup failed for %s", user_id, exc_info=True)
        return None
    return active.session_id if active else ""


async def _assert_manual_reply_allowed(user_id: str, conv, expected_session_id: str) -> None:
    """Increment 8B §8 — server-side gate raised BEFORE any DB write or external send so a
    rejected manual reply/resend sends nothing and writes nothing. Identical for /admin and
    /admin-v2 (both POST the SAME routes) and shared by `send` and `resend`:
      * archived conversation -> 409 (the v2 composer is also hidden for archived — backstop);
      * pilot conversation whose active session can't be verified (lookup error) -> 503,
        FAIL CLOSED — we refuse rather than risk sending into a stale/replaced session;
      * stale tab -> 409: the browser was rendered against `expected_session_id` but a
        different session (or none) is active now (e.g. the tester ran /newtest/reset).
    A legacy conversation with no session model never trips the 503/409 session checks
    (its `expected_session_id` is always "" and its lookup yields "")."""
    if conv.archived:
        raise HTTPException(status_code=409, detail="conversation is archived")
    current = await _active_session_id(user_id)
    if current is None:
        # Lookup failed. Fail CLOSED only for a genuinely session-aware pilot conversation;
        # a legacy conversation objectively has no session to verify, so it stays available.
        if _is_pilot_conversation(user_id, conv):
            raise HTTPException(status_code=503, detail="session check unavailable")
        return
    # A non-empty expected_session_id is only ever emitted by a pilot render that had an
    # active session, so any mismatch (including current=="" — the session was replaced)
    # is a stale tab. Empty expected (legacy / dev-demo) skips the check.
    if expected_session_id and expected_session_id != current:
        raise HTTPException(status_code=409, detail="session changed")


async def _render_conversation(user_id: str, request: Request, manager: dict):
    panel = get_conversation_store()
    conv = await panel.get(user_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    name = conv.qualification.get("name")
    conv.phone = conv.phone or conv.user_id   # старые карточки без phone → ключ как номер
    # Кем занят, если не нами (мягкое предупреждение — не блок).
    busy_by = conv.assigned_to if conv.assigned_to and conv.assigned_to != manager["login"] else ""
    return templates.TemplateResponse(request, "_conversation.html", {
        "c": conv,
        "initials": _initials(name, conv.phone),
        "avatar": _avatar(conv.phone),
        "manager": manager,
        "busy_by": busy_by,
        "outcomes": OUTCOMES,
        "quick_replies": quick_replies_for(conv.funnel),
        "qualification_rows": _qualification_rows(conv.qualification),
        "stage_label": dict(BOARD_COLUMNS).get(STAGE_TO_COLUMN.get(conv.stage, ""), conv.stage),
        # §8: which pilot session this render is bound to — the composer echoes it back on
        # send so the server can reject a stale tab (see _assert_manual_reply_allowed).
        # None (lookup outage) coerces to "" so rendering never breaks.
        "session_id": await _active_session_id(user_id) or "",
    })


@router.get("/conversation/{user_id}", response_class=HTMLResponse)
async def conversation(user_id: str, request: Request, manager: dict = Depends(require_admin)):
    """HTMX-партиал: полный контекст диалога + квалификация + действия менеджера."""
    return await _render_conversation(user_id, request, manager)


def _normalize_user_ids(user_ids: list[str], user_ids_csv: str = "") -> list[str]:
    items: list[str] = []
    for value in user_ids:
        items.extend(part.strip() for part in value.split(","))
    if user_ids_csv:
        items.extend(part.strip() for part in user_ids_csv.split(","))
    return [item for item in dict.fromkeys(items) if item]


@router.post("/conversations/archive", response_class=HTMLResponse)
async def archive_conversations(request: Request, manager: dict = Depends(require_admin),
                                user_ids: list[str] = Form(default=[]),
                                user_ids_csv: str = Form(default="")):
    """Soft-hide a batch of conversations and return the refreshed inbox partial."""
    ids = _normalize_user_ids(user_ids, user_ids_csv)
    panel = get_conversation_store()
    count = await panel.set_archived_many(ids, True)
    await panel.add_audit(manager["login"], "archive_many", "", f"count={count}")
    return await _render_inbox_partial(request)


@router.post("/conversations/archive-noise", response_class=HTMLResponse)
async def archive_noise_conversations(request: Request, manager: dict = Depends(require_admin)):
    """Archive all current noise conversations using the same card model as the inbox."""
    models = await _all_models(_now())
    ids = [m["user_id"] for m in models if m["is_noise"]]
    panel = get_conversation_store()
    count = await panel.set_archived_many(ids, True)
    await panel.add_audit(manager["login"], "archive_noise", "", f"count={count}")
    return await _render_inbox_partial(request)


@router.post("/conversation/{user_id}/archive", response_class=JSONResponse)
async def archive_conversation(user_id: str, manager: dict = Depends(require_admin)):
    """Soft-hide a conversation from boards, inbox, search and counters."""
    panel = get_conversation_store()
    if await panel.get(user_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    await panel.set_archived(user_id, True)
    await panel.add_audit(manager["login"], "archive", user_id)
    return JSONResponse({"ok": True})


@router.post("/conversation/{user_id}/unarchive", response_class=JSONResponse)
async def unarchive_conversation(user_id: str, manager: dict = Depends(require_admin)):
    """Return a conversation from archive."""
    panel = get_conversation_store()
    if await panel.get(user_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    await panel.set_archived(user_id, False)
    await panel.add_audit(manager["login"], "unarchive", user_id)
    return JSONResponse({"ok": True})


@router.post("/conversation/{user_id}/takeover", response_class=HTMLResponse)
async def takeover(user_id: str, request: Request, manager: dict = Depends(require_admin)):
    """Менеджер перехватывает диалог: бот замолкает, диалог закрепляется за менеджером."""
    await _set_intercept(user_id, True)
    await get_conversation_store().update_meta(user_id, assigned_to=manager["login"])
    await get_conversation_store().add_audit(manager["login"], "takeover", user_id)
    return await _render_conversation(user_id, request, manager)


@router.post("/conversation/{user_id}/release", response_class=HTMLResponse)
async def release(user_id: str, request: Request, manager: dict = Depends(require_admin)):
    """Вернуть диалог боту (снять перехват и закрепление)."""
    await _set_intercept(user_id, False)
    await get_conversation_store().release_claim(user_id)
    await get_conversation_store().add_audit(manager["login"], "release", user_id)
    return await _render_conversation(user_id, request, manager)


@router.post("/conversation/{user_id}/send", response_class=HTMLResponse)
async def send_message(user_id: str, request: Request, manager: dict = Depends(require_admin),
                       text: str = Form(""), expected_session_id: str = Form("")):
    """Менеджер отвечает клиенту прямо из панели. Ручная отправка авто-перехватывает диалог.

    Increment 8B §8 (server-side manual-reply safety, identical for /admin и /admin-v2 —
    оба шлют СЮДА): before anything is written or sent we assert the conversation exists,
    is NOT archived, and is still the CURRENT active pilot session (`expected_session_id`
    is the hidden field both composers echo back — a stale tab after /newtest is rejected
    with 409). ONLY after every check passes do we intercept, log the pending message and
    call the channel. The external send stays OUTSIDE any open DB transaction — each
    panel-store call opens/commits its own — so a channel stall can never hold a row lock,
    and a rejected reply leaves NO intercept / assigned_to / pending message behind."""
    text = text.strip()
    panel = get_conversation_store()
    conv = await panel.get(user_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    await _assert_manual_reply_allowed(user_id, conv, expected_session_id)
    if text:
        await _set_intercept(user_id, True)  # отвечает человек → бот молчит
        await panel.update_meta(user_id, assigned_to=manager["login"])
        msg_id = await panel.add_message(user_id, "manager", text, status="pending")
        try:
            provider = await outbound.send_to_client(
                conv.channel, conv.bot_id, conv.chat_id or user_id, text)
            await panel.mark_message_status(message_id=msg_id, status="sent",
                                            set_provider_msg_id=(provider or None))
        except Exception:  # noqa: BLE001 — не теряем сообщение в логе при сбое канала
            await panel.mark_message_status(message_id=msg_id, status="failed")
            log.warning("manager send failed (channel=%s)", conv.channel, exc_info=True)
        await panel.add_audit(manager["login"], "send", user_id, text[:120])
    return await _render_conversation(user_id, request, manager)


@router.post("/conversation/{user_id}/resend/{message_id}", response_class=HTMLResponse)
async def resend(user_id: str, message_id: int, request: Request,
                 manager: dict = Depends(require_admin),
                 expected_session_id: str = Form("")):
    """Повторить отправку сообщения, помеченного failed.

    Increment 8B §8: повтор — это тоже внешняя отправка, поэтому он проходит ТОТ ЖЕ
    серверный контракт, что и ручной ответ (`_assert_manual_reply_allowed`), ПЛЮС серверную
    проверку самого сообщения (не полагаемся на то, что кнопка «Повторить» видна только у
    failed). Строгий порядок, всё ДО вызова канала:
      1. найти Conversation (иначе 404);
      2. найти сообщение В ЭТОЙ Conversation — `conv.messages` содержит только её
         сообщения, поэтому чужой/несуществующий id → 404 (принадлежность + существование);
      3. общий archived/session guard (`_assert_manual_reply_allowed`);
      4. сообщение исходящее (bot/manager, не входящее client) — иначе 409;
      5. текущий пилот допускает повтор ТОЛЬКО при status == "failed" (sent/delivered/read/
         pending/прочее → 409, ничего не отправляем и не меняем).
    Только после всех проверок — transport.send. При любом отказе: канал не вызывается,
    статус не меняется, новые сообщения не создаются, takeover/lead_status не трогаются."""
    panel = get_conversation_store()
    conv = await panel.get(user_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    target = next((m for m in conv.messages if m.id == message_id), None)
    if target is None:
        # Сообщения нет в ЭТОЙ переписке — либо не существует, либо принадлежит другой
        # Conversation. В обоих случаях отклоняем без внешней отправки.
        raise HTTPException(status_code=404, detail="message not found in this conversation")
    await _assert_manual_reply_allowed(user_id, conv, expected_session_id)
    if target.sender not in ("bot", "manager"):
        raise HTTPException(status_code=409, detail="message is not outgoing")
    if target.status != "failed":
        # Повтор допустим только для недоставленного сообщения — уже sent/delivered/read/
        # pending повторно НЕ отправляем (иначе дубль клиенту).
        raise HTTPException(status_code=409, detail="message is not in a resendable state")
    if target.text:
        try:
            provider = await outbound.send_to_client(
                conv.channel, conv.bot_id, conv.chat_id or user_id, target.text)
            await panel.mark_message_status(message_id=message_id, status="sent",
                                            set_provider_msg_id=(provider or None))
        except Exception:  # noqa: BLE001
            await panel.mark_message_status(message_id=message_id, status="failed")
            log.warning("resend failed (channel=%s)", conv.channel, exc_info=True)
        await panel.add_audit(manager["login"], "resend", user_id)
    return await _render_conversation(user_id, request, manager)


@router.post("/conversation/{user_id}/suggest", response_class=PlainTextResponse)
async def suggest_reply(user_id: str, request: Request, _: dict = Depends(require_admin)):
    """Сгенерировать черновик ответа клиенту (Claude) из контекста — менеджер правит и шлёт."""
    conv = await get_conversation_store().get(user_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    if not llm_enabled():
        return "ИИ недоступен (нет ключа OpenRouter) — ответьте вручную."
    # История диалога → формат чата (client=user, bot/manager=assistant).
    history = [{"role": "user" if m.sender == "client" else "assistant", "content": m.text}
               for m in conv.messages if m.text]
    if not history or history[-1]["role"] != "user":
        history.append({"role": "user", "content": "(Предложи уместный следующий шаг.)"})
    persona = "приёмная комиссия Intellect IT & Business College"
    system = (
        f"Ты — менеджер {persona}. Предложи ОДИН следующий ответ клиенту по контексту "
        f"переписки: тепло, кратко, по-русски, без выдуманных фактов. "
        f"Контекст для тебя: {conv.ai_summary or '—'}. Следующий шаг: {conv.manager_next_step or '—'}. "
        f"Верни ТОЛЬКО текст ответа клиенту, без пояснений."
    )
    try:
        resp = await chat(system, history)
        text = " ".join(b.get("text", "") for b in resp.get("content", [])
                        if b.get("type") == "text").strip()
        return text or "Не удалось сгенерировать черновик — попробуйте ещё раз."
    except Exception:  # noqa: BLE001
        log.warning("suggest failed", exc_info=True)
        return "Не удалось сгенерировать черновик — попробуйте ещё раз."


@router.post("/conversation/{user_id}/stage", response_class=PlainTextResponse)
async def set_stage(user_id: str, manager: dict = Depends(require_admin),
                    stage: str = Form(...)):
    """Ручной перенос карточки в другую колонку канбана (drag-and-drop менеджером)."""
    target = COLUMN_TO_STAGE.get(stage)
    if target is None:
        raise HTTPException(status_code=400, detail="unknown column")
    await get_conversation_store().update_meta(user_id, stage=target)
    await get_conversation_store().add_audit(manager["login"], "stage", user_id, target)
    return PlainTextResponse("ok")


@router.post("/conversation/{user_id}/outcome", response_class=HTMLResponse)
async def set_outcome(user_id: str, request: Request, manager: dict = Depends(require_admin),
                      outcome: str = Form(...)):
    """Менеджер отмечает исход диалога (оплатил / дошёл / слился)."""
    valid = {key for key, _ in OUTCOMES}
    if outcome in valid:
        await get_conversation_store().update_meta(user_id, outcome=outcome)
        await get_conversation_store().add_audit(manager["login"], "outcome", user_id, outcome)
    return await _render_conversation(user_id, request, manager)


async def _set_intercept(user_id: str, value: bool) -> None:
    # Источник правды для глушения бота — DialogState.intercepted (его читает оркестратор).
    store = get_state_store()
    state = await store.load(user_id)
    state.intercepted = value
    await store.save(state)
    # Отражаем в карточке панели.
    await get_conversation_store().set_intercepted(user_id, value)

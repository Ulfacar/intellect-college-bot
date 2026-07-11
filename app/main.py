"""Точка входа FastAPI: вебхуки каналов + healthcheck."""
from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager

from collections import OrderedDict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from app.channels.bitrix_openlines import BitrixOpenLinesAdapter, bot_id_from_event, nest_form
from app.channels.telegram import TelegramAdapter
from app.channels.wappi import (
    WappiAdapter,
    is_delivery_status,
    is_incoming_user_message,
    parse_delivery_status,
)
from app.config import BotConfig, settings
from app.core import observ
from app.core.bots import registry
from app.core.orchestrator import Orchestrator
from app.integrations.panel.store import get_conversation_store

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Создаём схему БД (идемпотентно), если используется Postgres под CRM или панель.
    if settings.crm_backend == "postgres" or settings.panel_backend == "postgres":
        from app.integrations.crm.db import init_db
        await init_db()
        log.info("Postgres: схема (сделки/диалоги) готова")
    try:
        from app.core.faq import seed_defaults
        await seed_defaults()
    except Exception:  # noqa: BLE001
        log.warning("FAQ defaults seed failed", exc_info=True)
    # Фоновые джобы: watchdog-алерты + автодожим. Автодожим регистрируем всегда —
    # джоба сама сверяется с рантайм-флагом (переключается кнопкой в админке без рестарта).
    from app.core import awaiting, followup, scheduler, watchdog
    scheduler.register("watchdog", watchdog.run)
    scheduler.register("awaiting", awaiting.run)
    scheduler.register("followup", followup.run)
    scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()


app = FastAPI(title="Intellect College Bot", lifespan=lifespan)
# Сессии менеджеров (подписанная cookie) — для логина в админ-панель.
# https_only=True ставит Secure-флаг (TLS терминирует nginx, ходим по https);
# same_site=lax — базовая защита от CSRF.
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret,
                   max_age=14 * 24 * 3600, https_only=True, same_site="lax")


def _verify_webhook(request: Request, *, telegram: bool = False) -> bool:
    """Проверка секрета входящего вебхука. Пустой settings.webhook_secret → пропускаем
    (обратная совместимость, чтобы не уронить прод до обновления URL у провайдера)."""
    expected = settings.webhook_secret
    if not expected:
        return True
    if telegram:
        got = request.headers.get("x-telegram-bot-api-secret-token", "")
    else:
        got = request.query_params.get("s", "") or request.headers.get("x-webhook-secret", "")
    return bool(got) and secrets.compare_digest(got, expected)

# Наблюдаемость «тишины» вебхуков живёт в app.core.observ (общий доступ с watchdog).
# Дедуп входящих Wappi по id события (повторная доставка вебхука не плодит ответы).
_seen_wappi_ids: "OrderedDict[str, None]" = OrderedDict()
_SEEN_MAX = 2000


def _seen_before(event_id: str) -> bool:
    """True, если событие с таким id уже обрабатывали (защита от дублей доставки)."""
    if not event_id:
        return False
    if event_id in _seen_wappi_ids:
        return True
    _seen_wappi_ids[event_id] = None
    if len(_seen_wappi_ids) > _SEEN_MAX:
        _seen_wappi_ids.popitem(last=False)
    return False

# Админ-панель (канбан диалогов + чат + перехват).
if settings.admin_enabled:
    from app.admin.router import router as admin_router
    app.include_router(admin_router)

# Дев-демо: одиночный admission-бот в Telegram. Поднимается только при заданном токене —
# продовый WhatsApp/Wappi-канал Telegram-токена не требует.
_telegram = TelegramAdapter() if settings.telegram_bot_token else None
_telegram_orchestrator = Orchestrator(channel=_telegram) if _telegram else None

# Тестовые Telegram-боты (песочница): по оркестратору на каждого, со своим токеном и
# ЖЁСТКИМ сценарием admission (как WhatsApp-боты). Маршрут — /webhook/telegram/<id>.
# Ключ диалога bot_id:user_id, поэтому тестовые боты не пересекаются.
_telegram_test: dict[str, tuple[TelegramAdapter, Orchestrator]] = {}
for _tb in settings.telegram_bots:
    _tg_bot = BotConfig(id=_tb.id, scenario=_tb.scenario, title=_tb.title)
    _tg_adapter = TelegramAdapter(token=_tb.token)
    _telegram_test[_tb.id] = (_tg_adapter, Orchestrator(channel=_tg_adapter, bot=_tg_bot))

# Прод: по оркестратору на каждого настроенного бота (свой канал + сценарий).
_bot_orchestrators: dict[str, Orchestrator] = {
    bot.id: Orchestrator(channel=BitrixOpenLinesAdapter(bot=bot), bot=bot)
    for bot in registry.all()
}

# Прямой WhatsApp через Wappi (Схема B, тест/MVP) — оркестратор на профиль с заданным id.
_wappi_orchestrators: dict[str, Orchestrator] = {
    bot.wappi_profile_id: Orchestrator(channel=WappiAdapter(bot=bot), bot=bot)
    for bot in registry.all()
    if bot.wappi_profile_id
}


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "last_inbound_seconds_ago": observ.last_inbound_ago(),
    }


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    if not _verify_webhook(request, telegram=True):
        return JSONResponse({"ok": False, "reason": "forbidden"}, status_code=403)
    if _telegram_orchestrator is None:
        return {"ok": False, "reason": "telegram_disabled"}
    raw = await request.json()
    msg = await _telegram.parse(raw)
    await _telegram_orchestrator.handle(msg)  # не-текст/перехват — внутри оркестратора
    return {"ok": True}


@app.post("/webhook/telegram/{bot_id}")
async def telegram_test_webhook(bot_id: str, request: Request):
    """Тестовый Telegram-бот (песочница): свой токен + жёсткий admission-сценарий."""
    if not _verify_webhook(request, telegram=True):
        return JSONResponse({"ok": False, "reason": "forbidden"}, status_code=403)
    entry = _telegram_test.get(bot_id)
    if entry is None:
        return JSONResponse({"ok": False, "reason": "unknown_bot"}, status_code=404)
    adapter, orchestrator = entry
    raw = await request.json()
    msg = await adapter.parse(raw)
    await orchestrator.handle(msg)
    return {"ok": True}


@app.post("/webhook/bitrix")
async def bitrix_webhook(request: Request) -> dict:
    """Единый эндпоинт Открытых линий: маршрут к нужному боту по BOT_ID события imbot.

    Bitrix шлёт событие form-urlencoded (`data[PARAMS][...]`); JSON принимаем тоже
    (тесты/ручная отладка). `nest_form` приводит оба к вложенному dict.
    """
    if not _verify_webhook(request):
        return JSONResponse({"ok": False, "reason": "forbidden"}, status_code=403)
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        flat: object = await request.json()
    else:
        flat = list((await request.form()).multi_items())
    event = nest_form(flat)

    bitrix_bot_id = bot_id_from_event(event)
    bot = registry.by_bitrix_bot_id(bitrix_bot_id) if bitrix_bot_id else None
    if bot is None:
        log.warning("Bitrix-событие без сопоставленного бота (BOT_ID=%s)", bitrix_bot_id)
        return {"ok": False, "reason": "unknown_bot"}

    orchestrator = _bot_orchestrators[bot.id]
    msg = await orchestrator.channel.parse(event)
    await orchestrator.handle(msg)
    return {"ok": True, "bot": bot.id}


@app.post("/webhook/wappi")
async def wappi_webhook(request: Request) -> dict:
    """Прямой WhatsApp-канал (Wappi). Маршрут к боту по profile_id события.

    Wappi оборачивает события в `{"messages": [ {...}, ... ]}`; обрабатываем каждое.
    Игнорируем не-входящие, наши эхо (`is_me`), реакции и групповые чаты — отвечаем
    только в личных диалогах, иначе бот ответит сам себе или зафлудит группу.
    """
    if not _verify_webhook(request):
        return JSONResponse({"ok": False, "reason": "forbidden"}, status_code=403)
    payload = await request.json()
    # Wappi: события в payload["messages"]; на всякий случай поддерживаем и плоский формат.
    events = payload.get("messages") if isinstance(payload, dict) else None
    if not events:
        events = [payload]

    handled = 0
    for raw in events:
        if not isinstance(raw, dict):
            continue

        # Статус доставки/прочтения нашего исходящего → обновляем галочку в панели.
        if is_delivery_status(raw):
            provider_msg_id, status = parse_delivery_status(raw)
            if provider_msg_id and status:
                try:
                    await get_conversation_store().mark_message_status(
                        provider_msg_id=provider_msg_id, status=status)
                except Exception:  # noqa: BLE001
                    log.warning("delivery-status update failed", exc_info=True)
            continue

        if not is_incoming_user_message(raw):
            continue

        if _seen_before(str(raw.get("id", ""))):
            continue  # дубль доставки вебхука — уже обработали

        profile_id = str(raw.get("profile_id", ""))
        orchestrator = _wappi_orchestrators.get(profile_id)
        if orchestrator is None:
            log.warning("Wappi-событие без сопоставленного бота (profile_id=%s)", profile_id)
            continue

        observ.note_inbound()
        msg = await orchestrator.channel.parse(raw)
        await orchestrator.handle(msg)
        handled += 1

    return {"ok": True, "handled": handled}

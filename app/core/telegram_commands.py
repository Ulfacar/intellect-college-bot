"""Telegram pilot commands + normal-message dispatcher (Increment 4 + Increment 5 FAQ).

Three responsibilities, kept together because they share the same routing decision
("is this text a command or a normal message, and who/what should answer it?"):

1. **Command parsing/dispatch** (`/newtest /reset /status /manager /bot /feedback /help`)
   — allowlist-only, routed BEFORE `Orchestrator.handle`. Command text NEVER reaches
   FAQ/OpenRouter and never changes `lead_status` except the explicit actions below.
2. **`route_message`** — the single entry point `app/main.py` calls for every allowed
   private Telegram update (kept thin per `docs/telegram-pilot-implementation-plan.md`):
   commands short-circuit here; normal messages get an active session ensured
   (auto-created on first contact, §11), then gated on canonical `dialog_owner`
   (manager/paused -> log to the legacy panel and stay silent), then on the effective
   bot on/off switch (`Orchestrator._bots_on()`, global AND individual — OFF -> log to
   the legacy panel and stay silent, exactly like the manager/paused gate, see
   `_try_faq_reply` note below), then on the managed FAQ / knowledge base
   (Increment 5, `app/core/faq_kb.py` + `app/core/faq_matcher.py`) BEFORE the
   orchestrator: a confident deterministic match answers directly (or hands off, for
   `handoff_only` entries) WITHOUT ever calling `orchestrator.handle` — no OpenRouter,
   no second Conversation/Lead, `lead_status` untouched. No match -> falls through to
   `orchestrator.handle` unchanged (existing LLM/legacy-FAQ pipeline).
3. **FAQ pipeline integration** (`_try_faq_reply`/`_send_faq_reply`) — reuses
   `get_conversation_store()` (legacy panel) directly for manager-visible logging of
   the incoming question and the sent answer/handoff phrase, exactly like
   `_log_to_legacy_panel` already does for the manager/paused gate and like
   `Orchestrator._reply` does for the LLM path — `orchestrator.py` itself is NEVER
   imported or modified here.

Ignored Telegram update types (callback_query/edited_message/non-private chats) are
filtered in `app/main.py` BEFORE this module is reached — see `app/channels/telegram.py`
`update_kind`/`chat_type` helpers.
"""
from __future__ import annotations

import logging
from typing import Any

from app.channels.base import Message
from app.core import telegram_sessions
from app.core.conversation_service import ConversationService
from app.integrations.panel.audit_store import get_audit_store
from app.integrations.panel.store import get_conversation_store

log = logging.getLogger("telegram_commands")

# Increment 5: safe phrase sent instead of the stored answer for a matched
# `handoff_only` FAQ entry — never the raw answer_ru/ky (see faq-knowledge-base-spec.md
# §5 and the Increment 5 design: matched + handoff_only never answers by itself).
FAQ_HANDOFF_SAFE_PHRASE = (
    "Этот вопрос лучше уточнит менеджер. Диалог передан сотруднику колледжа."
)

COMMANDS: set[str] = {"/newtest", "/reset", "/status", "/manager", "/bot", "/feedback", "/help"}

HELP_TEXT = (
    "Доступные команды тест-пилота:\n"
    "/newtest — начать новую тестовую сессию (история сохраняется)\n"
    "/reset — сбросить текущую сессию и начать заново\n"
    "/status — показать текущее состояние сессии\n"
    "/manager — имитировать запрос менеджера (бот замолкает)\n"
    "/bot — вернуть диалог боту\n"
    "/feedback <текст> — оставить комментарий тестировщика\n"
    "/help — этот список"
)

UNKNOWN_COMMAND_TEXT = (
    "Неизвестная команда. Список доступных команд — /help."
)


# --------------------------------------------------------------------------------------
# Command parsing (channel-agnostic text parsing — no Telegram-specific bits here).
# --------------------------------------------------------------------------------------

def is_command(text: str) -> bool:
    return text.strip().startswith("/")


def parse_command(text: str) -> tuple[str, str]:
    """`"/feedback  ответ неточный"` -> `("/feedback", "ответ неточный")`. Strips a
    Telegram `@BotUsername` suffix (`/status@my_bot` -> `/status`) and lowercases the
    command word only (args keep original case)."""
    stripped = text.strip()
    parts = stripped.split(maxsplit=1)
    cmd = parts[0].lower()
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    args = parts[1] if len(parts) > 1 else ""
    return cmd, args


# --------------------------------------------------------------------------------------
# Command handlers — each returns the reply text (str). No network/adapter calls here,
# so these are unit-testable without a fake Telegram bot.
# --------------------------------------------------------------------------------------

async def _cmd_newtest(bot_id: str, external_user_id: str, external_chat_id: str) -> str:
    await telegram_sessions.start_new_session(
        bot_id, external_user_id, external_chat_id=external_chat_id,
    )
    return (
        "Новая тестовая сессия начата. История прошлой сессии сохранена и доступна "
        "в панели. Статус: new, бот отвечает с чистого листа."
    )


async def _cmd_reset(bot_id: str, external_user_id: str, external_chat_id: str) -> str:
    await telegram_sessions.start_new_session(
        bot_id, external_user_id, external_chat_id=external_chat_id,
    )
    return "Сессия сброшена. Начинаем диалог заново."


async def _cmd_status(bot_id: str, external_user_id: str, external_chat_id: str) -> str:
    session = await telegram_sessions.ensure_active_session(
        bot_id, external_user_id, external_chat_id=external_chat_id,
    )
    prefix = ""
    if session.created:
        prefix = "Активной сессии не было — создана новая.\n\n"
    snap = telegram_sessions.build_status_snapshot(session.conversation, session.lead)
    return prefix + _format_status(snap)


def _format_status(snap: dict[str, Any]) -> str:
    q = snap["qualification"]
    lines = [
        f"bot_id: {snap['bot_id']}",
        f"session: {snap['session_id'][:12]}",
        f"lead_status: {snap['lead_status']}",
        f"bot_phase: {snap['bot_phase']}",
        f"dialog_owner: {snap['dialog_owner']}",
        f"lead_temperature: {snap['lead_temperature']}",
        f"qualification: name={q['name'] or '—'}, grade_base={q['grade_base'] or '—'}, "
        f"direction={q['direction'] or '—'}",
    ]
    if snap["suggested_status"]:
        lines.append(f"suggested_status: {snap['suggested_status']}")
    lines.append(f"assigned_to: {snap['assigned_to'] or '—'}")
    if snap["next_action_type"]:
        lines.append(f"next_action: {snap['next_action_type']} at {snap['next_action_at']}")
    return "\n".join(lines)


async def _cmd_manager(bot_id: str, external_user_id: str, external_chat_id: str) -> str:
    session = await telegram_sessions.ensure_active_session(
        bot_id, external_user_id, external_chat_id=external_chat_id,
    )
    service = ConversationService()
    await service.request_manager(
        session.conversation.id, actor=f"telegram_tester:{external_user_id}",
        reason="/manager command",
    )
    return "Запрос менеджера зафиксирован — бот замолкает в этом диалоге, дождитесь ответа человека."


async def _cmd_bot(bot_id: str, external_user_id: str, external_chat_id: str) -> str:
    session = await telegram_sessions.ensure_active_session(
        bot_id, external_user_id, external_chat_id=external_chat_id,
    )
    service = ConversationService()
    await service.return_to_bot(
        session.conversation.id, actor=f"telegram_tester:{external_user_id}",
        reason="/bot command",
    )
    return "Диалог возвращён боту."


async def _cmd_feedback(bot_id: str, external_user_id: str, external_chat_id: str, args: str) -> str:
    comment = args.strip()
    if not comment:
        return "Напишите комментарий после команды, например: /feedback ответ неточный"
    session = await telegram_sessions.ensure_active_session(
        bot_id, external_user_id, external_chat_id=external_chat_id,
    )
    lead = session.lead
    await get_audit_store().record(
        lead_id=(lead.id if lead else None), conversation_id=session.conversation.id,
        event_type="test_note", metadata={"comment": comment}, source="telegram_test",
        actor=f"telegram_tester:{external_user_id}",
    )
    return "Комментарий сохранён. Полная оценка ответа появится на следующем этапе пилота."


async def handle_command(
    *, bot_id: str, external_user_id: str, external_chat_id: str, command: str, args: str,
) -> str:
    """Dispatch a parsed command to its handler. Unknown command (or a command word not
    in `COMMANDS`) -> short help text, no LLM/FAQ involved either way."""
    if command not in COMMANDS:
        return UNKNOWN_COMMAND_TEXT
    if command == "/help":
        return HELP_TEXT
    if command == "/newtest":
        return await _cmd_newtest(bot_id, external_user_id, external_chat_id)
    if command == "/reset":
        return await _cmd_reset(bot_id, external_user_id, external_chat_id)
    if command == "/status":
        return await _cmd_status(bot_id, external_user_id, external_chat_id)
    if command == "/manager":
        return await _cmd_manager(bot_id, external_user_id, external_chat_id)
    if command == "/bot":
        return await _cmd_bot(bot_id, external_user_id, external_chat_id)
    return await _cmd_feedback(bot_id, external_user_id, external_chat_id, args)


# --------------------------------------------------------------------------------------
# route_message — single entry point called from app/main.py webhook handler.
# --------------------------------------------------------------------------------------

async def _log_to_legacy_panel(msg: Message, bot_id: str, orchestrator: Any) -> None:
    """Same call `Orchestrator._log_in` makes — used whenever the orchestrator is
    deliberately NOT invoked for an otherwise-normal message (`dialog_owner` is
    manager/paused, the effective on/off switch is OFF) so the incoming message still
    shows up live in the admin panel."""
    try:
        panel = get_conversation_store()
        key = f"{bot_id}:{msg.user_id}" if bot_id else msg.user_id
        await panel.add_message(
            key, "client", msg.text, channel=msg.channel, bot_id=bot_id,
            chat_id=msg.chat_id, phone=msg.user_id,
        )
        bot_cfg = getattr(orchestrator, "bot", None)
        if bot_cfg is not None:
            await panel.update_meta(key, funnel=bot_cfg.scenario)
    except Exception:  # noqa: BLE001 — лог не критичен для диалога
        log.warning("panel log_in (manager/paused/off/faq gate) failed", exc_info=True)


def _extract_client_message_id(msg: Message) -> str | None:
    """Best-effort Telegram `message_id` for `faq_kb_answer_log.client_message_id`
    (nullable — absence is fine, see the answer-log contract in
    `app/core/faq_kb.py`)."""
    raw_msg = (msg.raw or {}).get("message") or {}
    mid = raw_msg.get("message_id")
    return str(mid) if mid is not None else None


async def _send_faq_reply(msg: Message, bot_id: str, adapter: Any, text: str) -> None:
    """Send + log a deterministic FAQ answer/handoff phrase — mirrors
    `Orchestrator._reply` (pending -> send -> sent/failed) so the legacy panel shows the
    same delivery-status lifecycle as the LLM path, without importing orchestrator.py."""
    panel = get_conversation_store()
    key = f"{bot_id}:{msg.user_id}" if bot_id else msg.user_id
    msg_id = 0
    try:
        msg_id = await panel.add_message(
            key, "bot", text, channel=msg.channel, bot_id=bot_id, status="pending", phone=msg.user_id,
        )
    except Exception:  # noqa: BLE001
        log.warning("faq panel log_out failed (bot=%s)", bot_id, exc_info=True)
    try:
        await adapter.send(msg.chat_id, text)
        if msg_id:
            await panel.mark_message_status(message_id=msg_id, status="sent")
    except Exception:  # noqa: BLE001 — сбой канала: помечаем failed, диалог не роняем
        if msg_id:
            try:
                await panel.mark_message_status(message_id=msg_id, status="failed")
            except Exception:  # noqa: BLE001
                pass
        log.warning("faq reply send failed (bot=%s)", bot_id, exc_info=True)


async def _try_faq_reply(
    msg: Message, *, bot_id: str, adapter: Any, orchestrator: Any, session: Any,
) -> bool:
    """Increment 5: try the managed FAQ / knowledge base BEFORE the orchestrator.

    Fails open (returns False -> caller falls through to `orchestrator.handle`) on any
    lookup/matching error — a bug in the FAQ layer must never silence the bot. Returns
    True iff a reply (or handoff) was actually sent, in which case the caller MUST NOT
    also call `orchestrator.handle` (no double reply, no second Conversation/Lead,
    `lead_status` untouched, no Trello outbox — see module docstring)."""
    from app.core import faq_kb, faq_matcher

    text = msg.text or ""
    try:
        store = faq_kb.get_faq_kb_store()
        candidates = await store.list_published_candidates()
        if not candidates:
            return False
        language = faq_matcher.detect_language(text)
        result = faq_matcher.match(text, candidates, language=language)
    except Exception:  # noqa: BLE001 — fail open to the normal LLM/legacy-FAQ pipeline
        log.warning("faq kb lookup/match failed (bot=%s)", bot_id, exc_info=True)
        return False
    if not result.matched:
        return False

    # Incoming question was never logged (orchestrator.handle is being skipped) — log it
    # now so the manager still sees it live, same as the manager/paused/off gates.
    await _log_to_legacy_panel(msg, bot_id, orchestrator)

    if result.handoff_only:
        # Matched + handoff_only: NEVER send the stored answer_ru/ky as the final
        # answer. Reuse the same request_manager the /manager command uses:
        # dialog_owner=manager, bot_phase=handoff, lead_status UNCHANGED, no outbox.
        await ConversationService().request_manager(
            session.conversation.id, actor="faq:handoff_only",
            reason=f"faq_entry={result.faq_entry_id}",
        )
        reply_text = FAQ_HANDOFF_SAFE_PHRASE
    else:
        reply_text = result.answer or ""

    await _send_faq_reply(msg, bot_id, adapter, reply_text)

    try:
        await faq_kb.get_faq_kb_store().log_answer(
            conversation_id=session.conversation.id,
            client_message_id=_extract_client_message_id(msg),
            bot_message_id=None,
            source="faq",
            faq_entry_id=result.faq_entry_id,
            faq_version_id=result.faq_version_id,
            matched_variant_id=result.matched_variant_id,
            match_type=result.match_type or "",
            match_score=result.score,
            language=result.language,
            missing_answer_ky=result.missing_answer_ky,
        )
    except Exception:  # noqa: BLE001 — best-effort: must never break the reply already sent
        log.warning("faq answer log write failed (bot=%s)", bot_id, exc_info=True)

    return True


async def route_message(msg: Message, *, bot_id: str, adapter: Any, orchestrator: Any) -> None:
    """Route one already-allowlisted, already-private Telegram `Message`.

    Commands are handled entirely here (never reach the orchestrator or FAQ). Normal
    messages get an active session ensured (auto-created on first contact) and are
    gated, IN ORDER: canonical `dialog_owner` (manager/paused -> legacy-panel log only,
    stay silent) -> effective bot on/off switch (`orchestrator._bots_on()`, global AND
    individual — OFF -> legacy-panel log only, stay silent; this check is duplicated
    here, rather than relying solely on the orchestrator's own internal check, so the
    managed FAQ layer below is ALSO gated by it, not just the LLM path) -> managed FAQ
    / knowledge base (Increment 5, deterministic, no LLM — a match answers/hands off
    and STOPS here) -> orchestrator (existing LLM/legacy-FAQ pipeline, unchanged)."""
    if not msg.user_id:
        return  # служебный/пустой апдейт — как в Orchestrator.handle, ничего не создаём
    text = msg.text or ""
    if msg.kind == "text" and not text:
        return  # пустой апдейт без содержимого — как в Orchestrator.handle
    if is_command(text):
        command, args = parse_command(text)
        reply = await handle_command(
            bot_id=bot_id, external_user_id=msg.user_id, external_chat_id=msg.chat_id,
            command=command, args=args,
        )
        try:
            await adapter.send(msg.chat_id, reply)
        except Exception:  # noqa: BLE001 — сбой отправки не должен откатывать созданную сессию
            log.warning("telegram command reply send failed (bot=%s)", bot_id, exc_info=True)
        return

    session = await telegram_sessions.ensure_active_session(
        bot_id, msg.user_id, external_chat_id=msg.chat_id, channel=msg.channel,
    )
    if session.conversation.dialog_owner in ("manager", "paused"):
        await _log_to_legacy_panel(msg, bot_id, orchestrator)
        return

    # Effective on/off switch (global AND individual) — checked defensively via
    # getattr: production `Orchestrator` always has `_bots_on`; minimal test fakes that
    # only implement `.handle()` are treated as "on" (they don't exercise the switch).
    bots_on_check = getattr(orchestrator, "_bots_on", None)
    if bots_on_check is not None and not await bots_on_check():
        await _log_to_legacy_panel(msg, bot_id, orchestrator)
        return

    if await _try_faq_reply(msg, bot_id=bot_id, adapter=adapter, orchestrator=orchestrator, session=session):
        return

    await orchestrator.handle(msg)

"""Оркестратор: принимает Message, ведёт диалог через нужную воронку, отвечает.

Параллельно ведёт персистентный лог диалога для админ-панели (карточка + сообщения):
входящие пишутся ВСЕГДА (в т.ч. при перехвате — чтобы менеджер видел новые реплики
клиента), исходящие — когда бот отвечает. Сбои лога не должны ронять ответ бота.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

from app.channels.base import ChannelAdapter, Message
from app.config import BotConfig, settings
from app.core.manager_brief import build_manager_brief
from app.core.observ import record_failure
from app.core.router import detect_funnel
from app.core.state import get_state_store
from app.funnels import get_funnel
from app.integrations.panel.store import get_conversation_store

log = logging.getLogger("orchestrator")

GREETING = (
    "Здравствуйте! 😊 Это Frunze Travel. "
    "Подскажите, что вас интересует — тур, виза или авиабилеты?"
)

# Авто-исход диалога из стадии (ручные won/lost не перетираются — см. store).
_OFFICE_STAGES = {"office", "office_consultation"}
_MANAGER_STAGES = {"manager", "manager_handoff"}


# Сериализация обработки в рамках одного диалога. Два быстрых сообщения клиента подряд —
# это два параллельных запроса FastAPI; без лока оба читают/пишут одно состояние и оба
# отвечают → гонка истории и дубли ответов (видели в проде). Лок на ключ диалога гонит
# их строго по очереди: второй ход видит уже обновлённую историю и состояние.
_key_locks: dict[str, asyncio.Lock] = {}


def _lock_for(key: str) -> asyncio.Lock:
    lock = _key_locks.get(key)
    if lock is None:
        lock = _key_locks.setdefault(key, asyncio.Lock())
    return lock


def _auto_outcome(stage: str) -> str:
    if stage in _OFFICE_STAGES:
        return "office"
    if stage in _MANAGER_STAGES:
        return "manager"
    return "in_progress"


def _default_manager_name(bot: BotConfig) -> str:
    if bot.manager_name:
        return bot.manager_name
    if bot.scenario == "visa":
        return "Медина"
    if bot.id.endswith("sezim") or "sezim" in bot.id.lower():
        return "Сезим"
    if bot.scenario == "tours":
        return "Адеми"
    return "Сезим"

NON_TEXT_FALLBACK = (
    "Голосовые сообщения пока не распознаём 🙏 Напишите, пожалуйста, словами — "
    "или скажите «нужен менеджер», и я позову человека."
)
# Мягкий фолбэк, если ход не удалось обработать (сбой LLM/инструмента) — клиент НЕ
# должен получать тишину или 500. Диалог не роняем, состояние сохраняем.
LLM_ERROR_FALLBACK = (
    "Секундочку, уточню детали и вернусь к вам 🙏"
)


class Orchestrator:
    """Ведёт диалог одного бота. Если `bot` задан, его сценарий жёстко определяет
    воронку (тур-боты не угадывают её по ключевым словам). Без `bot` (дев-демо в
    Telegram) воронка определяется keyword-детектом, как раньше.
    """

    def __init__(self, channel: ChannelAdapter, bot: BotConfig | None = None) -> None:
        self.channel = channel
        self.bot = bot
        # Дебаунс: буфер быстрых реплик клиента и таймер тихого окна на диалог. В пределах
        # процесса (как и _lock_for) — допущение «один воркер / sticky», как у остального стейта.
        self._buffers: dict[str, list[Message]] = {}
        self._timers: dict[str, asyncio.Task] = {}

    @property
    def _bot_id(self) -> str:
        return self.bot.id if self.bot else ""

    def _key(self, msg: Message) -> str:
        """Ключ диалога: <bot_id>:<номер>. Один номер у разных ботов = разные диалоги
        (раздельные состояние/перехват/карточка). В дев-демо без bot — просто номер."""
        return f"{self._bot_id}:{msg.user_id}" if self.bot else msg.user_id

    async def _bots_on(self) -> bool:
        """Рубильник авто-ответов (флаг в БД, переключается из панели).

        Per-bot ключ `bots_enabled:<bot_id>` переопределяет глобальный `bots_enabled`:
        позволяет включить тест-ботов в Telegram, не будя боевой WhatsApp (его боты
        персональный ключ не задают → наследуют глобальный, сейчас OFF)."""
        from app.core import flags
        global_on = await flags.get_flag("bots_enabled", True)
        if self._bot_id:
            return await flags.get_flag(f"bots_enabled:{self._bot_id}", global_on)
        return global_on

    async def handle(self, msg: Message) -> None:
        if not msg.user_id:
            return  # служебный/пустой апдейт
        key = self._key(msg)

        # Не-текст (голос/фото/медиа): бот не распознаёт — логируем и сразу честный fallback,
        # без дебаунса (склеивать нечего).
        if msg.kind == "non_text":
            async with _lock_for(key):
                await self._handle_non_text(msg)
            return

        if not msg.text:
            return  # пустой апдейт без содержимого

        # Входящее логируем СРАЗУ (вне обработки) — менеджер видит реплику живьём, даже если
        # перехвачено / рубильник off / идёт окно дебаунса.
        await self._log_in(msg, msg.text)

        # Дебаунс выключен (0) — синхронная обработка под локом, как раньше.
        if settings.debounce_seconds <= 0:
            async with _lock_for(key):
                await self._run_turn(msg)
            return

        # Дебаунс включён: копим быстрые реплики клиента и перезапускаем таймер тихого окна.
        # По его истечении обработаем склеенный текст одним ходом LLM (без задвоений ответов).
        self._buffers.setdefault(key, []).append(msg)
        old = self._timers.get(key)
        if old is not None:
            old.cancel()
        self._timers[key] = asyncio.create_task(self._debounce_flush(key))

    async def _handle_non_text(self, msg: Message) -> None:
        """Голос/медиа: лог + честный фолбэк (бот не распознаёт). Под локом диалога."""
        await self._log_in(msg, "[медиа/голос]")
        state = await get_state_store().load(self._key(msg))
        if state.intercepted or not await self._bots_on():
            return  # перехвачено / рубильник off — лог записали, бот молчит
        await self._reply(msg, NON_TEXT_FALLBACK)

    async def _debounce_flush(self, key: str) -> None:
        """По истечении тихого окна склеить буфер и обработать одним ходом."""
        try:
            await asyncio.sleep(settings.debounce_seconds)
        except asyncio.CancelledError:
            return  # пришла новая реплика — этот таймер заменён свежим
        async with _lock_for(key):
            msgs = self._buffers.pop(key, [])
            self._timers.pop(key, None)
            if not msgs:
                return
            combined = "\n".join(m.text for m in msgs if m.text)
            combined_msg = replace(msgs[-1], text=combined)
            try:
                await self._run_turn(combined_msg)
            except Exception:  # noqa: BLE001 — фон: не роняем процесс, входящие уже в логе
                log.error("debounce flush failed (key=%s)", key, exc_info=True)

    async def _run_turn(self, msg: Message) -> None:
        """Обработать ход (выбор воронки → LLM → ответ) для уже залогированного входящего.

        Вызывается ПОД локом диалога: синхронно из handle (дебаунс off) либо из
        _debounce_flush со склеенным текстом. Входящее в панель уже записано в handle.
        """
        key = self._key(msg)
        store = get_state_store()
        state = await store.load(key)
        if self.bot is not None:
            state.bot_id = self.bot.id
            state.manager_name = _default_manager_name(self.bot)

        # Главный рубильник: если авто-ответы выключены из панели — бот молчит во всех
        # воронках (сообщение клиента уже в логе, менеджер ведёт диалог вручную).
        if not await self._bots_on():
            return

        # Перехват: бот молчит во всех воронках. НО при авто-хендоффе (stage=manager), пока
        # менеджер не подключился, один раз честно подтверждаем клиенту, что запрос передан и
        # когда ответят — иначе клиент висит в тишине («Алло… когда звонок?» по 15 сообщений).
        if state.intercepted:
            if state.stage == "manager":
                await self._maybe_wait_ack(msg, state, store)
            return

        # Выбор воронки, если ещё не определена.
        if state.funnel is None:
            if self.bot is not None:
                state.funnel = self.bot.scenario  # сценарий бота фиксирует воронку
            else:
                detected = detect_funnel(msg.text)
                if detected is None:
                    await self._reply(msg, GREETING)
                    await store.save(state)
                    return
                state.funnel = detected

        faq_reply = await self._maybe_faq_reply(msg, state, store)
        if faq_reply:
            return

        funnel = get_funnel(state.funnel)
        try:
            reply = await funnel.handle(msg, state)
        except Exception:  # noqa: BLE001 — сбой LLM/инструмента: не роняем вебхук, мягкий фолбэк
            log.error("funnel handle failed (key=%s)", key, exc_info=True)
            record_failure("llm")
            await store.save(state)               # сохраняем то, что успело накопиться в ходе
            await self._reply(msg, LLM_ERROR_FALLBACK)
            return

        # Передача менеджеру = бот замолкает (решение заказчика 23.06.2026): прощальную
        # реплику этого хода ещё отправляем, но дальше в этом чате отвечает только человек.
        # Менеджер видит карточку в «У менеджера» и может «Вернуть боту» из панели.
        auto_handoff = state.stage == "manager"
        if auto_handoff:
            state.intercepted = True

        # Перехват «на лету»: менеджер мог нажать «Перехватить», пока генерировался ответ.
        # Перечитываем свежее состояние; если перехвачено не нами (не хендофф) — не отвечаем.
        fresh = await store.load(key)
        intercepted_midflight = fresh.intercepted and not auto_handoff
        if intercepted_midflight:
            state.intercepted = True

        await store.save(state)
        await self._sync_card(msg, state)
        if reply and not intercepted_midflight:
            await self._reply(msg, reply)
        elif intercepted_midflight:
            log.info("reply dropped: intercepted mid-flight (key=%s)", key)

    async def _maybe_wait_ack(self, msg: Message, state, store) -> None:
        """Разовое подтверждение клиенту после авто-хендоффа, пока менеджер молчит.

        Не вмешиваемся, если менеджер уже подключился (закрепил диалог или ответил) —
        тогда бот молчит, чтобы не говорить поверх человека. Анти-спам: один ack на период
        ожидания; флаг сбрасывается, когда менеджер отвечает (новая пауза → новое ack)."""
        from app.core.branding import wait_ack_for
        try:
            conv = await get_conversation_store().get(self._key(msg))
        except Exception:  # noqa: BLE001 — нет данных панели: лучше промолчать
            return
        manager_engaged = bool(conv and (conv.assigned_to or any(
            m.sender == "manager" for m in conv.messages)))
        if manager_engaged:
            if state.wait_ack_sent:        # менеджер на связи — сбросим, чтобы при новой
                state.wait_ack_sent = False  # паузе подтвердить заново
                await store.save(state)
            return
        if state.wait_ack_sent:
            return  # уже подтвердили — дальше о брошенном клиенте напомнит awaiting-джоба
        await self._reply(msg, wait_ack_for(state.funnel))
        state.wait_ack_sent = True
        await store.save(state)

    # ---- лог панели (сбои глушим, чтобы не ронять бота) ----
    async def _log_in(self, msg: Message, text: str) -> None:
        try:
            panel = get_conversation_store()
            await panel.add_message(self._key(msg), "client", text, channel=msg.channel,
                                    bot_id=self._bot_id, chat_id=msg.chat_id, phone=msg.user_id)
            if self.bot is not None:
                await panel.update_meta(self._key(msg), funnel=self.bot.scenario)
        except Exception:  # noqa: BLE001 — лог не критичен для диалога
            log.warning("panel log_in failed", exc_info=True)

    async def _reply(self, msg: Message, text: str) -> None:
        # Логируем исходящее как pending → шлём → отмечаем доставку (sent/failed).
        panel = get_conversation_store()
        msg_id = 0
        try:
            msg_id = await panel.add_message(self._key(msg), "bot", text,
                                             channel=msg.channel, bot_id=self._bot_id,
                                             status="pending", phone=msg.user_id)
        except Exception:  # noqa: BLE001
            log.warning("panel log_out failed", exc_info=True)
        try:
            provider = await self.channel.send(msg.chat_id, text)
            if msg_id:
                await panel.mark_message_status(message_id=msg_id, status="sent",
                                                set_provider_msg_id=(provider or None))
        except Exception:  # noqa: BLE001 — сбой канала: помечаем failed, диалог не роняем
            record_failure("send")
            if msg_id:
                try:
                    await panel.mark_message_status(message_id=msg_id, status="failed")
                except Exception:  # noqa: BLE001
                    pass
            log.warning("bot send failed (channel=%s)", msg.channel, exc_info=True)

    async def _sync_card(self, msg: Message, state) -> None:
        try:
            panel = get_conversation_store()
            brief = build_manager_brief(state)
            await panel.update_meta(self._key(msg), funnel=state.funnel, stage=state.stage,
                                    qualification=state.qualification,
                                    outcome=_auto_outcome(state.stage), **brief)
        except Exception:  # noqa: BLE001
            log.warning("panel sync_card failed", exc_info=True)

    async def _maybe_faq_reply(self, msg: Message, state, store) -> bool:
        """Try deterministic FAQ before LLM/funnel logic. Fail open to the normal flow."""
        if state.funnel == "visa":
            try:
                from app.core.visa_pricing import self_visa_reply, visa_price_reply
                answer = visa_price_reply(msg.text)
                if answer is None:
                    answer = self_visa_reply(
                        msg.text,
                        already_sent=bool(state.qualification.get("self_visa_retention_sent")),
                    )
                    if answer and state.qualification.get("self_visa_retention_sent"):
                        state.stage = "manager"
                        state.intercepted = True
                    elif answer:
                        state.qualification["self_visa_retention_sent"] = True
                if answer:
                    state.history.append({"role": "user", "content": msg.text})
                    state.history.append({"role": "assistant", "content": answer})
                    await store.save(state)
                    await self._sync_card(msg, state)
                    await self._reply(msg, answer)
                    return True
            except Exception:  # noqa: BLE001
                log.warning("visa deterministic reply failed", exc_info=True)

        try:
            from app.core.faq import get_faq_store, match_faq, qualification_question
            faq_store = get_faq_store()
            entries = await faq_store.candidates(state.funnel)
            faq = match_faq(
                msg.text, state.funnel, entries, pending_field=state.pending_field
            )
        except Exception:  # noqa: BLE001
            log.warning("faq lookup failed", exc_info=True)
            return False
        if faq is None:
            return False

        answer = faq.answer
        pending_question = None
        if state.pending_field and faq.allow_during_qualification:
            pending_question = qualification_question(state.funnel, state.pending_field)
            if pending_question:
                answer = f"{answer}\n\n{pending_question}"

        if faq.handoff_only:
            state.stage = "manager"
            state.intercepted = True

        state.history.append({"role": "user", "content": msg.text})
        state.history.append({"role": "assistant", "content": answer})
        await store.save(state)
        await self._sync_card(msg, state)
        await self._reply(msg, answer)
        return True

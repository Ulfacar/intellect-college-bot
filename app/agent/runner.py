"""Агентный цикл: LLM ведёт диалог и вызывает инструменты воронки.

Универсальный `run_turn(state, user_text, spec)` переиспользуется всеми воронками —
без копирования цикла. Конкретика воронки (промпт, набор инструментов, исполнитель
инструментов) живёт в `FunnelSpec`. История диалога — в `DialogState.history`
(внутренний формат сообщений совместим с прежним агентным циклом).
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

import httpx

from app.agent.llm import client
from app.agent.prompts.tickets import SYSTEM as TICKETS_SYSTEM
from app.agent.prompts.tours import SYSTEM as TOURS_SYSTEM, system_for_manager as tours_system_for_manager
from app.agent.prompts.visa import SYSTEM as VISA_SYSTEM
from app.agent.tools import tools_for
from app.agent.validator import validate_reply
from app.config import settings
from app.core import observ
from app.core.branding import GETVISA_EMAIL, GETVISA_OFFICE_ADDRESS, PRICE_DISCLAIMER
from app.core.state import DialogState
from app.core.visa_pricing import self_visa_reply, visa_price_reply
from app.funnels.visa import score_visa, visa_category
from app.integrations.crm import get_crm
from app.integrations.tourvisor.client import TourVisorClient, TourVisorError

logger = logging.getLogger("agent.runner")

MAX_TOOL_ITERATIONS = 6
_tourvisor = TourVisorClient()

ToolExec = Callable[[str, dict, DialogState, object], Awaitable[str]]


@dataclass
class FunnelSpec:
    """Описание воронки для агентного цикла."""
    name: str
    system: str
    tools: list[dict]
    exec_tool: ToolExec


async def run_turn(state: DialogState, user_text: str, spec: FunnelSpec) -> str | None:
    """Обработать один ход клиента через LLM-агента (общий цикл для всех воронок)."""
    if state.intercepted:
        return None  # менеджер перехватил диалог — бот молчит
    state.history.append({"role": "user", "content": user_text})
    crm = get_crm()

    for _ in range(MAX_TOOL_ITERATIONS):
        resp = await client().messages.create(
            model=settings.llm_model_main,
            max_tokens=1024,
            system=spec.system,
            tools=spec.tools,
            messages=state.history,
        )

        if resp.stop_reason == "tool_use":
            state.history.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    out = await spec.exec_tool(block.name, dict(block.input), state, crm)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
            state.history.append({"role": "user", "content": results})
            continue

        text = "".join(b.text for b in resp.content if b.type == "text")
        # Валидатор: чиним безопасное (markdown, дисклеймер цен туров), мягко логируем риски.
        text, violations = validate_reply(text, spec.name)
        if violations:
            logger.info("validator (%s): %s", spec.name, ", ".join(violations))
            for v in violations:
                observ.note_validation(v)
        state.history.append({"role": "assistant", "content": text})
        return text or "Расскажите, пожалуйста, подробнее."

    return "Давайте уточню детали ещё раз, чтобы подобрать лучший вариант."


# ---------------- Туры ----------------
async def _tours_exec_tool(name: str, args: dict, state: DialogState, crm) -> str:
    logger.info("tours tool %s args=%s", name, args)

    if name == "search_tours":
        state.qualification.update({k: v for k, v in args.items() if v})
        if not state.deal_id:
            state.deal_id = await crm.create_lead({"user_id": state.user_id}, "tours", state.qualification)
        try:
            tours = await _tourvisor.search(state.qualification)
        except (TourVisorError, httpx.HTTPError):
            return ("Поиск туров сейчас временно недоступен. Я записал ваш запрос — "
                    "менеджер подберёт варианты и свяжется с вами.")
        return "\n".join(tours) if tours else "Подходящих туров не нашлось."

    if name == "handoff_to_manager":
        if state.deal_id:
            await crm.update_stage(state.deal_id, "manager_handoff")
        state.stage = "manager"
        return ("Передано менеджеру. Скажи клиенту КОРОТКО и честно: запрос передал(а) "
                "менеджеру, он ответит в этом чате; НЕ утверждай, что менеджер уже онлайн.")

    if name == "escalate_to_office":
        state.qualification.update({
            k: v for k, v in args.items()
            if k in {"name", "visit_time", "office_visit", "selected_option"} and v
        })
        client_name = args.get("name") or state.qualification.get("name")
        visit_time = (
            args.get("visit_time")
            or state.qualification.get("visit_time")
            or state.qualification.get("office_visit")
        )
        if not client_name:
            return (
                "Клиент хочет в офис, но имя ещё не собрано. НЕ вызывай офис как записанный "
                "визит и НЕ говори «менеджер уже ждёт». Сначала ответь на текущий вопрос клиента "
                "по туру, затем спроси: «Как могу к вам обращаться, чтобы менеджер понимал, "
                "по какой заявке вы придёте?»"
            )
        if not visit_time:
            return (
                "Имя клиента уже есть, но время визита не подтверждено. НЕ говори «менеджер уже "
                "ждёт». Спроси, на какое время завтра/в выбранный день клиенту удобно подойти."
            )
        if state.deal_id:
            await crm.update_stage(state.deal_id, "office_consultation")
        state.stage = "office"
        return (
            "Визит можно подтверждать. Коротко зафиксируй имя, время и выбранный вариант; "
            "дай адрес офиса, если клиент его ещё не получил. Паспорт упомяни мягко: для брони "
            "лучше взять загранпаспорта. Не утверждай, что менеджер уже ждёт."
        )

    return "ok"


TOURS_SPEC = FunnelSpec(
    name="tours",
    system=TOURS_SYSTEM,
    tools=tools_for(["search_tours", "handoff_to_manager", "escalate_to_office"]),
    exec_tool=_tours_exec_tool,
)


async def run_tours_turn(state: DialogState, user_text: str) -> str | None:
    """Один ход клиента в воронке «Туры»."""
    spec = TOURS_SPEC
    if state.manager_name:
        spec = replace(TOURS_SPEC, system=tours_system_for_manager(state.manager_name))
    return await run_turn(state, user_text, spec)


# ---------------- Визы ----------------
async def _visa_exec_tool(name: str, args: dict, state: DialogState, crm) -> str:
    logger.info("visa tool %s args=%s", name, args)

    if name == "score_visa":
        state.qualification.update({k: v for k, v in args.items() if v})
        if not state.deal_id:
            state.deal_id = await crm.create_lead({"user_id": state.user_id}, "visa", state.qualification)
        await crm.update_stage(state.deal_id, "visa_scoring")
        category = visa_category(score_visa(state.qualification))
        # Категория — ВНУТРЕННИЙ ориентир для тона. Клиенту НЕ обещаем визу/процент,
        # всегда ведём на консультацию (escalate_to_office).
        return (f"[внутренний сигнал силы кейса: {category}] Не называй клиенту процент и не "
                f"обещай визу. Подай мягко и честно (грамотная анкета и подготовка к интервью "
                f"решают многое) и пригласи на консультацию в офис или онлайн. Цену услуги "
                f"называй по официальному прайсу только если клиент спросил; депозиты/итоговую "
                f"сумму — не называй.")

    if name == "handoff_to_manager":
        if state.deal_id:
            await crm.update_stage(state.deal_id, "manager_handoff")
        state.stage = "manager"
        return ("Передано менеджеру. Скажи клиенту КОРОТКО и честно: запрос передал(а) "
                "менеджеру, он ответит в этом чате; НЕ утверждай, что менеджер уже онлайн.")

    if name == "escalate_to_office":
        if state.deal_id:
            await crm.update_stage(state.deal_id, "office_consultation")
        state.stage = "office"
        return (f"Пригласи клиента на консультацию. Адрес офиса: {GETVISA_OFFICE_ADDRESS}. "
                f"Можно начать и онлайн. Почта для документов: {GETVISA_EMAIL}. Цену услуги "
                f"называй по официальному прайсу только если клиент спросил; депозиты/итоговую "
                f"сумму — не называй.")

    return "ok"


VISA_SPEC = FunnelSpec(
    name="visa",
    system=VISA_SYSTEM,
    tools=tools_for(["score_visa", "escalate_to_office", "handoff_to_manager"]),
    exec_tool=_visa_exec_tool,
)


async def run_visa_turn(state: DialogState, user_text: str) -> str | None:
    """Один ход клиента в воронке «Визы»."""
    price_reply = visa_price_reply(user_text)
    if price_reply:
        state.history.append({"role": "user", "content": user_text})
        state.history.append({"role": "assistant", "content": price_reply})
        return price_reply

    retention = self_visa_reply(
        user_text,
        already_sent=bool(state.qualification.get("self_visa_retention_sent")),
    )
    if retention:
        state.history.append({"role": "user", "content": user_text})
        state.history.append({"role": "assistant", "content": retention})
        if state.qualification.get("self_visa_retention_sent"):
            state.stage = "manager"
            state.intercepted = True
        else:
            state.qualification["self_visa_retention_sent"] = True
        return retention

    return await run_turn(state, user_text, VISA_SPEC)


# ---------------- Билеты ----------------
async def _tickets_exec_tool(name: str, args: dict, state: DialogState, crm) -> str:
    logger.info("tickets tool %s args=%s", name, args)

    if name == "submit_request":
        state.qualification.update({k: v for k, v in args.items() if v})
        if not state.deal_id:
            state.deal_id = await crm.create_lead({"user_id": state.user_id}, "tickets", state.qualification)
        await crm.update_stage(state.deal_id, "manager_handoff")
        state.stage = "manager"
        return (f"Заявка передана менеджеру на подбор рейса и оплату. Скажи клиенту, что "
                f"менеджер пришлёт варианты и цену. {PRICE_DISCLAIMER} Цену сам не называй.")

    return "ok"


TICKETS_SPEC = FunnelSpec(
    name="tickets",
    system=TICKETS_SYSTEM,
    tools=tools_for(["submit_request"]),
    exec_tool=_tickets_exec_tool,
)


async def run_tickets_turn(state: DialogState, user_text: str) -> str | None:
    """Один ход клиента в воронке «Билеты»."""
    return await run_turn(state, user_text, TICKETS_SPEC)

"""Generic LLM agent loop plus the admission funnel spec."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from app.agent.llm import client
from app.agent.prompts.admission import SYSTEM as ADMISSION_SYSTEM, system_for_manager
from app.agent.tools import tools_for
from app.agent.validator import validate_reply
from app.config import settings
from app.core import observ
from app.core.state import DialogState
from app.funnels.admission import REQUIRED_FIELDS
from app.integrations.crm import get_crm

logger = logging.getLogger("agent.runner")

MAX_TOOL_ITERATIONS = 6
ToolExec = Callable[[str, dict, DialogState, object], Awaitable[str]]


@dataclass
class FunnelSpec:
    name: str
    system: str
    tools: list[dict]
    exec_tool: ToolExec


async def run_turn(state: DialogState, user_text: str, spec: FunnelSpec) -> str | None:
    if state.intercepted:
        return None
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
        text, violations = validate_reply(text, spec.name)
        if violations:
            logger.info("validator (%s): %s", spec.name, ", ".join(violations))
            for v in violations:
                observ.note_validation(v)
        state.history.append({"role": "assistant", "content": text})
        return text or "Расскажите, пожалуйста, подробнее."

    return "Давайте уточню детали ещё раз, чтобы не ошибиться."


async def _admission_exec_tool(name: str, args: dict, state: DialogState, crm) -> str:
    logger.info("admission tool %s args=%s", name, args)

    if name == "ask_qualification":
        data = {k: v for k, v in args.items() if k in {"name", "grade_base", "direction"} and v}
        had_data = bool(data)
        state.qualification.update(data)
        if had_data and not state.deal_id:
            state.deal_id = await crm.create_lead({"user_id": state.user_id}, "admission", state.qualification)
        state.stage = "qualification"
        state.pending_field = args.get("field") or None
        missing = [f for f in REQUIRED_FIELDS if f not in state.qualification]
        if not missing:
            state.pending_field = None
            return (
                "Все поля квалификации собраны (имя, база, направление). Больше анкетных "
                "вопросов НЕ задавай. Ответь на текущий вопрос клиента, если он есть, и предложи "
                "следующий шаг: запись на вступительный тест (escalate_to_office) или передачу менеджеру."
            )
        return (
            f"Записал. Ещё не собрано: {missing}. Задай ОДИН вопрос про «{args.get('field')}» — "
            "сначала коротко ответь на вопрос клиента, если он его задал. Не спрашивай два поля сразу."
        )

    if name == "escalate_to_office":
        state.qualification.update({
            k: v for k, v in args.items()
            if k in {"name", "grade_base", "direction", "visit_time"} and v
        })
        client_name = args.get("name") or state.qualification.get("name")
        if not client_name:
            return (
                "Клиент готов на тест, но имя не собрано. НЕ подтверждай запись. Сначала ответь "
                "на текущий вопрос клиента, затем спроси, как к нему обращаться."
            )
        if not state.deal_id:
            state.deal_id = await crm.create_lead({"user_id": state.user_id}, "admission", state.qualification)
        await crm.update_stage(state.deal_id, "test_invite")
        state.stage = "test_invite"
        return (
            "Зафиксировано: клиент приглашён на вступительный тест. Скажи коротко: заявку на тест "
            "передал менеджеру приёмной, он свяжется в этом чате и подтвердит дату, время и формат. "
            "НЕ называй сам дату/время/формат теста и проходной балл — порядок записи подтверждает "
            "менеджер. Напомни, что тест по математике и английскому, длительность 1,5 часа, и что "
            "персональная скидка обсуждается после теста (размер не называй)."
        )

    if name == "handoff_to_manager":
        reason = args.get("reason")
        if reason:
            state.qualification["escalation_reason"] = reason
        if not state.deal_id and state.qualification:
            state.deal_id = await crm.create_lead({"user_id": state.user_id}, "admission", state.qualification)
        if state.deal_id:
            await crm.update_stage(state.deal_id, "manager_handoff")
        state.stage = "manager"
        return (
            "Передано менеджеру приёмной комиссии. Скажи клиенту КОРОТКО и честно: вопрос "
            "передал менеджеру, он ответит в этом чате; НЕ утверждай, что менеджер уже онлайн, "
            "и НЕ отвечай сам на вопрос, из-за которого эскалируешь."
        )

    if name == "crm_update_stage":
        stage = args.get("stage")
        if stage not in {"qualification", "consulting", "test_invite"}:
            return "Недопустимая стадия, ничего не менял."
        state.stage = stage
        if state.deal_id:
            await crm.update_stage(state.deal_id, stage)
        return f"Стадия обновлена: {stage}. Продолжай диалог."

    return "ok"


ADMISSION_SPEC = FunnelSpec(
    name="admission",
    system=ADMISSION_SYSTEM,
    tools=tools_for(["ask_qualification", "handoff_to_manager", "escalate_to_office", "crm_update_stage"]),
    exec_tool=_admission_exec_tool,
)


async def run_admission_turn(state: DialogState, user_text: str) -> str | None:
    spec = ADMISSION_SPEC
    if state.manager_name:
        spec = replace(ADMISSION_SPEC, system=system_for_manager(state.manager_name))
    return await run_turn(state, user_text, spec)

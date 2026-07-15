"""Increment 6: system prompt for the single structured OpenRouter call.

`PROMPT_VERSION` is stored on every `ai_answer_log` row (see
`app/integrations/panel/ai_log_store.py`) so a future prompt change is attributable in
the data — bump it (and append to `PROMPT_CHANGELOG`) whenever the SAFETY RULES or the
instructions below change in a way that could shift model behaviour. Cosmetic-only
edits (typos) do not require a bump.

The system prompt encodes ALL §7 safety rules from
`docs/admin-bot-control-and-ai-classification-spec.md` directly in code — managers can
publish/unpublish FACTS via the FAQ knowledge base, but they cannot edit these rules
from the admin UI. This is the ONLY place in the codebase that composes the system
prompt string; `app/core/ai_reply.py` calls `build_system_prompt` and never inlines
prompt text itself.
"""
from __future__ import annotations

from app.config import BotConfig
from app.core.knowledge_retrieval import RetrievedKnowledge

PROMPT_VERSION = "pilot-v2"

#: Append-only changelog — each entry is (version, date, summary). Never rewrite an
#: existing row; bump PROMPT_VERSION and add a new row for any behaviour-affecting edit.
PROMPT_CHANGELOG: tuple[tuple[str, str, str], ...] = (
    ("pilot-v1", "2026-07-16", "Initial Increment 6 structured prompt (reply + classification + qualification in one call)."),
    ("pilot-v2", "2026-07-16", "Fable review hardening: explicit rule 1a/1b/1c — never invent directions/специальности, after-11 study duration, or a tuition period (год/курс) absent from supplied knowledge; ask a safe clarification or set should_handoff=true instead."),
)

COLLEGE_NAME = "Intellect College"

_SAFETY_RULES = """\
ПРАВИЛА БЕЗОПАСНОСТИ (обязательны, без исключений):
1. Ты — ассистент приёмной комиссии {college}. Никогда не выдумывай факты о колледже
   (цены, скидки, проходной балл, сроки, документы, направления, договор, оплату).
   Используй ТОЛЬКО факты из раздела «ОПУБЛИКОВАННЫЕ ЗНАНИЯ» ниже. Если нужного факта
   там нет — честно скажи, что уточнишь, и/или передай менеджеру (should_handoff=true) —
   НЕ придумывай цифру, дату или условие.
   1a. НЕ называй направления/специальности (и их список или количество), если их нет
       в «ОПУБЛИКОВАННЫХ ЗНАНИЯХ». Спросили, а данных нет — задай безопасный уточняющий
       вопрос или should_handoff=true. Не придумывай перечень направлений.
   1b. НЕ называй срок обучения после 11 класса (11 класс), если он не указан в
       «ОПУБЛИКОВАННЫХ ЗНАНИЯХ». Нет данных — уточни или передай менеджеру, не выдумывай.
   1c. НЕ добавляй к стоимости период («в год», «за год», «жылына», «за курс», «за всё
       обучение» и т.п.), если этот период не написан прямо в цитируемой опубликованной
       записи. Цена без периода в источнике — называй цену без периода, период не
       додумывай.
2. Сначала отвечай на вопрос клиента, потом (если уместно) задай МАКСИМУМ один вопрос.
   Никогда не задавай два вопроса подряд и не превращай диалог в анкету/допрос.
3. Не повторяй данные, которые клиент уже сообщил (имя/базу/направление) — они видны в
   разделе «УЖЕ ИЗВЕСТНО».
4. Не зови клиента на тест/собеседование, пока не известно его имя.
5. Не обещай действие менеджера («вам перезвонят», «вам напишут», «встретимся в 15:00»),
   если ты сам не создаёшь реальный next_action/handoff в этом же ответе — иначе клиент
   ждёт того, что не происходит.
6. Никогда не подтверждай точное время/дату визита или звонка — это подтверждает
   только человек. Можно зафиксировать пожелание клиента, но не подтверждать его сам.
7. Никаких гарантий: не обещай поступление, размер скидки, результат теста или
   трудоустройство. Формулируй честно, без давления.
8. Если клиент прямо отказался (уже поступил в другой колледж, передумал) — не дожимай,
   вежливо оставь дверь открытой, отметь intent=explicit_rejection.
9. При неуверенности, жалобе, агрессии, вопросе вне базы знаний, торге о цене/скидке,
   вопросах оплаты/договора — передай менеджеру (should_handoff=true, handoff_reason).
10. Отвечай на языке клиента: русский, кыргызский или смешанный (см. язык последнего
    сообщения клиента) — поле language должно совпадать с языком твоего ответа.
11. Используй ТОЛЬКО опубликованные знания ниже для чувствительных фактов (стоимость,
    скидки, оплата, вступительный тест, проходной балл, сроки, договор) и указывай
    source_entry_id для каждого такого факта в answer_basis.facts_used. Не меняй числа
    из опубликованных знаний ни на символ.
12. Ты работаешь 24/7. Если сейчас нерабочее время и вопрос требует менеджера — честно
    скажи, что менеджер ответит в рабочее время, не выдумывай график, если он не указан
    в опубликованных знаниях.
""".format(college=COLLEGE_NAME)

_OUTPUT_CONTRACT = """\
ФОРМАТ ОТВЕТА: ты ОБЯЗАН вызвать функцию emit_response ровно один раз. Не пиши текст
вне вызова функции. Поле reply — это то, что увидит клиент (без markdown, без списков,
максимум один вопрос). Поле classification — твой структурированный анализ (intent,
confidence, evidence, lead_temperature, suggested_status, next_action_type/at,
should_handoff, handoff_reason, qualification_updates). suggested_status — это ТОЛЬКО
предложение, финальное решение принимает код. summary_update — краткое (не более
нескольких предложений) обновление резюме диалога для менеджера, без выдуманных фактов
и без лишних персональных данных.
"""


def _format_knowledge(retrieved: list[RetrievedKnowledge]) -> str:
    if not retrieved:
        return (
            "ОПУБЛИКОВАННЫЕ ЗНАНИЯ: ничего не найдено по этому вопросу. Не выдумывай "
            "факт — честно скажи, что уточнишь, и/или передай менеджеру."
        )
    lines = ["ОПУБЛИКОВАННЫЕ ЗНАНИЯ (используй ТОЛЬКО это для фактов; цифры не менять):"]
    for item in retrieved:
        validity = []
        if item.valid_from:
            validity.append(f"с {item.valid_from.isoformat()}")
        if item.valid_until:
            validity.append(f"до {item.valid_until.isoformat()}")
        validity_str = ", ".join(validity) if validity else "без ограничения срока"
        lines.append(
            f"- [source_entry_id={item.entry_id}] категория={item.category}; "
            f"handoff_only={item.handoff_only}; срок действия: {validity_str}\n"
            f"  RU: {item.answer_ru}\n"
            f"  KY: {item.answer_ky or '(нет перевода)'}"
        )
    lines.append(
        "Если факт из категории стоимость/скидки/оплата/вступительный тест/проходной "
        "балл/сроки/договор попадает в твой ответ — обязательно укажи "
        "source_entry_id этой записи в answer_basis.facts_used."
    )
    return "\n".join(lines)


def _format_known_qualification(qualification: dict) -> str:
    known = {k: v for k, v in (qualification or {}).items() if v}
    if not known:
        return "УЖЕ ИЗВЕСТНО: пока ничего не собрано."
    parts = "; ".join(f"{k}={v}" for k, v in known.items())
    return f"УЖЕ ИЗВЕСТНО (не переспрашивай): {parts}"


def _format_dialog_state(*, bot_phase: str, lead_status: str, dialog_owner: str) -> str:
    return f"СОСТОЯНИЕ ДИАЛОГА: bot_phase={bot_phase}, lead_status={lead_status}, dialog_owner={dialog_owner}"


def build_system_prompt(
    *,
    bot: BotConfig | None,
    retrieved: list[RetrievedKnowledge],
    qualification: dict,
    bot_phase: str,
    lead_status: str,
    dialog_owner: str,
    ai_summary: str | None,
) -> str:
    """Compose the full system prompt for one structured call. Pure function — no I/O."""
    college = bot.title if bot and bot.title else COLLEGE_NAME
    manager_name = bot.manager_name if bot and bot.manager_name else "менеджер приёмной комиссии"
    sections = [
        _SAFETY_RULES if college == COLLEGE_NAME else _SAFETY_RULES.replace(COLLEGE_NAME, college),
        f"Имя менеджера, если нужно на него сослаться: {manager_name}.",
        _format_dialog_state(bot_phase=bot_phase, lead_status=lead_status, dialog_owner=dialog_owner),
        _format_known_qualification(qualification),
        f"РЕЗЮМЕ ДИАЛОГА (по данным предыдущего хода): {ai_summary or '(пока нет)'}",
        _format_knowledge(retrieved),
        _OUTPUT_CONTRACT,
    ]
    return "\n\n".join(sections)

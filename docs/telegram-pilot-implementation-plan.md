# Telegram-пилот — план реализации

> **Цель:** безопасный тестовый контур в Telegram (без Wappi/WhatsApp/Trello/Tilda/прода), где
> сотрудники колледжа проверяют бота, оценивают ответы и правят FAQ без кода. Бизнес-логика —
> идентична будущему WhatsApp. Trello = stub/outbox-заглушка.
>
> **Ветка:** `feature/telegram-pilot` (в `main` не мержим). Реализация — инкрементами, на каждом
> прогон тестов + diff-summary. Секреты только в `.env`, реальные токены/номера/переписки не
> коммитим.

---

## 1. Аудит текущего состояния (факт)

| Подсистема | Есть сейчас | Файл | Пробел под пилот |
|---|---|---|---|
| **Telegram adapter** | parse update→Message (text/non_text), send; ленивый Bot; мульти-бот через `/webhook/telegram/{bot_id}` | `channels/telegram.py`, `main.py:103-161` | нет allowlist, нет `callback_query` (inline-кнопки), нет команд, нет дедупа `update_id`, нет reply_markup |
| **Мульти-бот** | `telegram_bots: list[TelegramBotConfig]` (id/token/title), оркестратор на бота, ключ `<bot_id>:<user_id>` | `config.py:23-27,55-56`, `main.py:109-113` | ок как основа; нужен счётчик диалогов/статус в UI |
| **Рубильники** | `bots_enabled` + `bots_enabled:<bot_id>` | `orchestrator.py:87-97`, `flags.py` | ⚠ **override**, требуется **AND** (задача X1) |
| **OpenRouter client** | OpenAI-совм. вызов, tools, `llm_enabled()` | `agent/llm.py` | нет логов токенов/стоимости/latency/модели; `usage` не читается; нет бюджет-режима |
| **FAQ** | CRUD/toggle/test, память+PG, live-чтение, seed | `core/faq.py`, `admin/router.py:450-519`, `faq.html` | нет `answer_ru/ky`, `is_published`, версий/отката, `valid_*`, delete, категорий, подтверждения |
| **Перехват** | takeover/release/ручная отправка (авто-intercept), проверка «на лету» | `admin/router.py:681-721`, `orchestrator.py:216-225`, `_set_intercept:799` | на `intercepted(bool)`; нужен `dialog_owner`(bot/manager/paused) |
| **Panel store** | `ConversationView` (диалог+карточка вместе), аудит, claim | `integrations/panel/store.py`, `crm/db.py` | нужно разделить Conversation/Lead + новые поля + Feedback/AnswerContext |
| **Классификация** | нет (есть `manager_brief` — резюме/температура эвристикой) | `core/manager_brief.py` | нужен структурированный AI-classifier |
| **LeadStatusService** | нет (есть `crm_update_stage`/`_auto_outcome`) | `agent/runner.py`, `orchestrator.py:43` | нужен единый сервис переходов + outbox-stub |
| **Тесты** | 21 файл, 136 зелёных (вкл. `test_telegram_routing`, `test_intercept`, `test_faq`, `test_llm_openrouter`) | `tests/` | добавить пилотные наборы (§8) |

**База:** `python -m pytest -q` → 136 passed (baseline ветки).

---

## 2. Что переиспользуем как есть
Telegram parse/send, мульти-бот реестр и webhook-роуты, дедуп Wappi как образец, FAQ-движок
(`match_faq`/`candidates`/live-чтение), аудит панели, `manager_brief` как основу AI-резюме,
дебаунс/локи оркестратора, стейт-стор, каркас тестов.

## 3. Чего не хватает (реализуем в пилоте)
1. **Allowlist** тестировщиков (env + гейт в вебхуке).
2. **Inline-feedback** (callback_query) + команды `/newtest /reset /status /feedback /manager`.
3. **Дедуп `update_id`** (по образцу `_seen_wappi_ids`).
4. **Рубильник AND** (исправить `_bots_on`, X1).
5. **`dialog_owner`** (bot/manager/paused) рядом с `intercepted`.
6. **`bot_phase` / `lead_status`** рядом со `stage`; **Conversation/Lead** разделение (пилотный минимум).
7. **AI-classifier** (структурированный JSON) + **LeadStatusService** (переходы + outbox-stub).
8. **OpenRouter логирование** (input/output tokens, cost, latency, model) + **бюджет-режим**.
9. **Feedback + AnswerContext** хранилище.
10. **Админка:** раздел «Тестирование / Ошибки бота», статус allowlist, статус бюджета AI.
11. **FAQ**: `answer_ru/ky`, draft/published, версии/откат, valid_*, категории (по FAQ-спеке).

---

## 4. Файлы, которые будут изменены/созданы

**Конфиг/каналы**
- `app/config.py` — `telegram_allowed_user_ids`, `telegram_allowed_chat_ids`, бюджет-поля,
  `llm_model_main/cheap` дефолты (значения — в `.env`).
- `app/channels/telegram.py` — parse `callback_query`, reply_markup на send, разбор команд.
- `.env.example` — новые переменные (только имена/плейсхолдеры, без секретов).

**Ядро**
- `app/core/allowlist.py` (нов.) — проверка user/chat id.
- `app/core/budget.py` (нов.) — учёт расходов LLM, дневной/месячный лимит, флаг блокировки.
- `app/core/leadstatus.py` (нов.) — `LeadStatusService` (переходы + `manual_status_lock_until` +
  outbox-stub).
- `app/core/telegram_commands.py` (нов.) — `/newtest /reset /status /feedback /manager`.
- `app/agent/llm.py` — читать `usage`, мерить latency, писать лог+бюджет.
- `app/agent/classifier.py` (нов.) — структурированная классификация (§9 ТЗ).
- `app/core/orchestrator.py` — гейты (allowlist/off/budget/dialog_owner), inline-кнопки к ответу.
- `app/core/flags.py` — не трогаем (флаги как есть).

**Данные/панель**
- `app/integrations/panel/store.py` — поля Conversation/Lead, `FeedbackView`, `AnswerContextView`.
- `app/integrations/crm/db.py` — таблицы `feedback`, `answer_context`, `llm_usage`,
  расширение под lead_status/bot_phase/dialog_owner (аддитивно, `stage`/`intercepted` остаются).
- `app/main.py` — telegram webhook: allowlist-гейт, дедуп `update_id`, роутинг команд и
  callback-feedback.

**Админка**
- `app/admin/router.py` — маршруты раздела «Тестирование/Ошибки», статус allowlist, статус бюджета,
  «Создать FAQ из ошибки», фильтры.
- `app/admin/templates/*` — `feedback.html`/`_feedback_card.html`, статус AI/бюджета, allowlist-вью.

**Тесты**
- `tests/test_telegram_allowlist.py`, `test_telegram_commands.py`, `test_telegram_feedback.py`,
  `test_switches_and.py`, `test_dialog_owner.py`, `test_leadstatus_service.py`,
  `test_classifier.py`, `test_llm_budget.py`, `test_faq_publish.py` (+ дополнения к существующим).

---

## 5. Модель данных (пилотный минимум, PostgreSQL)

**Conversation** (диалог): `conversation_id, channel(telegram), bot_id, telegram_user_id,
telegram_chat_id, phone(nullable), bot_phase(greeting|qualification|consultation|waiting|handoff),
dialog_owner(bot|manager|paused), assigned_to, lead_id(nullable), last_text, last_sender,
last_message_at`.

**Lead** (карточка): `lead_id, name, phone(nullable), telegram_username, grade_base, direction,
lead_status(11 ключей), lead_source(=telegram_test), lead_temperature(new|cold|warm|hot),
suggested_status, next_action_type, next_action_at, ai_summary, escalation_reason, qualification,
manual_status_lock_until, status_change_source/by/reason`.

**Feedback** (оценка тестировщика): `id, conversation_id, client_message_id, bot_message_id,
bot_id, telegram_tester_id, rating(correct|inaccurate|wrong|need_push|over_push|need_manager|
comment), comment, expected_answer, expected_intent, expected_status, expected_handoff, created_at,
reviewed_by, review_status(new|reviewed|fixed)`.

**AnswerContext** (тех.контекст ответа): `id, conversation_id, bot_message_id, source(faq|llm),
faq_rule_id, prompt_version, model, input_tokens, output_tokens, cost, latency_ms, confidence,
intent, evidence, suggested_status, applied_status, lead_temperature, bot_phase, dialog_owner,
validator_violations[]`.

**LlmUsage** (учёт бюджета): `id, ts, bot_id, model, input_tokens, output_tokens, cost, latency_ms`.

**BudgetState** (или вычисляемо из LlmUsage): суточный/месячный расход, флаг `ai_blocked` + причина.

> `stage`/`intercepted` остаются для совместимости (бэкофилл), `lead_status` — канонический в
> Lead/PostgreSQL, `DialogState` не второй источник истины (см. `phase1-implementation-plan.md`).

---

## 6. Маршруты и шаблоны
- Webhook: `POST /webhook/telegram/{bot_id}` (есть) + allowlist-гейт, дедуп, callback-feedback.
- Admin: `GET /admin/testing` (раздел ошибок, фильтры), `POST /admin/feedback/{id}/review`,
  `POST /admin/feedback/{id}/faq` («Создать FAQ из ошибки»), `GET /admin/testing/allowlist`,
  статус бюджета в `/admin/system`.
- Шаблоны: `testing.html`, `_feedback_card.html`, блок AI/бюджета в `system.html`, allowlist-вью.

## 7. Команды Telegram (только allowlist)
`/newtest` — завершить сессию (сохранить историю) → новый Conversation+Lead, qualification очищен,
`bot_phase=greeting`, `lead_status=new`, `dialog_owner=bot`; `/status` — текущее тест-состояние;
`/reset` — сброс сессии; `/feedback <текст>` — комментарий; `/manager` — имитировать запрос
менеджера (handoff).

---

## 8. Новые тесты (сводно; детально — §19 ТЗ)
Telegram (allowlist/неизвестный/1 бот/3 бота/маршрут/команды/inline-feedback/дедуп `update_id`);
рубильники (global OFF гасит всех; global ON+individual OFF; OFF не зовёт OpenRouter; OFF сохраняет
входящее; OFF разрешает ручной ответ); перехват (takeover не меняет lead_status; ручной ответ
перехватывает; release; отмена ответа при перехвате «на лету»); FAQ (published отвечает без LLM;
draft/disabled/expired не используются; live без рестарта; RU/KY; handoff_only; rollback);
классификация (intent/confidence/evidence/suggested_status; безопасные авто-статусы; manager-only;
low-confidence; manual lock; validator violations); feedback (привязка к правильному ответу;
комментарий; FAQ из ошибки; фильтры; повторный тест; не смешивается с обычным сообщением);
бюджет (стоимость логируется; при лимите LLM блокируется, FAQ и ручные ответы работают).
**Все 136 существующих тестов остаются зелёными.**

---

## 9. Порядок реализации (инкременты, каждый с тестами)
1. **Env/конфиг:** allowlist, бюджет-поля, модель в `.env.example` (без секретов).
2. **Рубильник AND** (`_bots_on`) + тесты (исправление X1).
3. **Telegram:** дедуп `update_id`, allowlist-гейт, команды `/status /reset /newtest /feedback
   /manager` + тесты.
4. **Три состояния** `bot_phase/lead_status/dialog_owner` рядом со `stage`/`intercepted` (аддитивно,
   бэкофилл) + перехват на `dialog_owner` + тесты.
5. **LeadStatusService** + переходы + outbox-stub + тесты.
6. **AI-classifier** (структурированный JSON) + маппинг intent→status через сервис + тесты.
7. **OpenRouter логирование** (tokens/cost/latency/model) + **бюджет-режим** + тесты.
8. **Feedback + AnswerContext** (хранилище + inline-кнопки + запись контекста) + тесты.
9. **Админка:** раздел «Тестирование/Ошибки», фильтры, «Создать FAQ из ошибки», статус бюджета.
10. **FAQ-апгрейд** (answer_ru/ky, draft/published, версии/откат, valid_*, категории) — по FAQ-спеке.

## 10. Риски
- **R1 Двойной источник истины** (DialogState vs Lead) — держать канонический `lead_status` в PG,
  DialogState не владеет статусом; бэкофилл-геттеры.
- **R2 Регресс каркаса** — все изменения аддитивные, 136 тестов как страховка; `stage`/`intercepted`
  не удаляем.
- **R3 Расходы OpenRouter** — бюджет-режим + OFF не зовёт LLM + FAQ до LLM; без ключа LLM не
  вызывается.
- **R4 Смешение feedback с обычным сообщением** — inline callback_query отделён от текстовых update.
- **R5 Приватность** — allowlist гейтит незнакомцев (не создаём карточку/не зовём LLM/не храним
  полный диалог); реальные данные не коммитим.
- **R6 Рубильник-семантика** — исправление override→AND может задеть текущее поведение; покрыть
  тестами до/после.
- **R7 Объём** — большой; реализуем инкрементами с зелёными тестами на каждом шаге.

## 11. Что можно сделать без дополнительных доступов (всё нужное для пилота)
Всё из §9 реализуемо локально: Telegram-песочница требует лишь **тестовые** bot-токены в `.env`
(их вводит владелец, в репо не попадают); OpenRouter требует `OPENROUTER_API_KEY` в `.env` (без
него LLM-часть просто не зовётся, FAQ и админка работают). **Не требуется:** Wappi, реальные
WhatsApp-номера, Trello key/token/webhook, Tilda, прод-деплой. PostgreSQL — локальный.

## 12. Ограничения фазы (жёстко)
Не подключать: Wappi, реальные WhatsApp-номера, Trello API/webhook, реальные карточки/данные,
авто-сообщения клиентам, Tilda, прод. Архитектурные решения из документации не менять. Telegram —
только песочница. `lead_source=telegram_test`, настоящих Trello-карточек не создавать.

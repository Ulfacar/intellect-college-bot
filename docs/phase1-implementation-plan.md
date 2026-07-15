# Phase 1 — план реализации (без Trello-доступов)

> **Статус:** согласованный план, **код ещё не пишется**. Trello API в Phase 1 **не подключается**
> (только stub/outbox-заглушка). Ничего не коммитим без отдельной команды.
>
> **Парные документы:** `admin-bot-control-and-ai-classification-spec.md`,
> `trello-status-sync-spec.md`, `faq-knowledge-base-spec.md`, `requirements-traceability.md`.

---

## 0. Инварианты Phase 1 (обязательны)

1. **Канонический `lead_status` хранится в Lead / PostgreSQL.** Это единственный источник истины
   для бизнес-статуса.
2. **`DialogState` НЕ является вторым источником истины** для статуса. Он держит `bot_phase` и
   (временно) диалоговое состояние; `lead_status` он не «владеет».
3. **`stage` и `intercepted` остаются временно** для обратной совместимости (бэкофилл), не
   удаляются в Phase 1.
4. **Outbox-событие создаётся только при реальной смене `lead_status`** (не при перехвате, не при
   смене `bot_phase`/`dialog_owner`, не при no-op).
5. **Никакого реального Trello API в Phase 1** — worker-заглушка (лог + `trello_sync_status=pending`).
6. **Общий рубильник имеет абсолютный приоритет:**
   `effective_bot_enabled = global_bots_enabled AND individual_bot_enabled`.
7. **Все изменения покрываются тестами; старые 136 тестов остаются зелёными.**

---

## 1. Шаги

### Шаг 1. Ввести три состояния рядом со старым `stage`
- Добавить `bot_phase` (greeting/qualification/consultation/waiting/handoff),
  `lead_status` (11 ключей), `dialog_owner` (bot/manager/paused).
- `lead_status` — на сущности **Lead** (PostgreSQL). `bot_phase`/`dialog_owner` — на Conversation.
- `stage`/`intercepted` **остаются**, помечены deprecated.

### Шаг 2. Обратная совместимость (бэкофилл)
- Чтение: если новые поля пусты → выводить из `stage`/`intercepted` (адаптеры-геттеры).
- Запись: смены пишутся в новые поля И зеркалятся в старые, пока UI/джобы не переведены.
- Критерий: существующие 136 тестов не падают.

### Шаг 3. `LeadStatusService` (единая точка смены `lead_status`)
- Единственный вход для смены `lead_status` (bot/admin/system; trello — Phase 2).
- Внутри: проверка разрешённого перехода (таблица §3 trello-спеки) → проверка
  `manual_status_lock_until` → запись в PostgreSQL → аудит (`status_change_source/by/reason`) →
  **если статус реально изменился** — постановка outbox-события.
- Пишет только в PostgreSQL; Trello — через outbox-заглушку.

### Шаг 4. Развести перехват и статус карточки
- `takeover`/`release`/ручная отправка (`admin/router.py:681-721`) → меняют **только**
  `dialog_owner`/`assigned_to`, **не** трогают `lead_status` (не создают outbox).
- Добавить `dialog_owner=paused` («Поставить на паузу»).
- Авто-хендофф (`orchestrator.py:209-211`) разделить: `bot_phase=handoff` + `dialog_owner=manager`
  отдельно от `lead_status`.
- Проверка перед AI-ответом (`orchestrator.py:216-225`) → читать `dialog_owner` вместо `intercepted`.

### Шаг 5. Рубильники трёх ботов — формула AND
- Изменить `_bots_on` (`orchestrator.py:87-97`) на **`global AND individual`**:
  общий OFF → все off; индивидуальный ON **не** включает при общем OFF; при общем ON индивидуальный
  OFF выключает только выбранного.
- ⚠ Это **исправление текущего поведения** (сейчас override) — единственное изменение семантики
  рубильника; покрыть тестами.
- При OFF: входящее сохраняется; авто-ответ, **AI-классификатор**, авто-`lead_status`, followup —
  не выполняются (OpenRouter не тратится); фоновая AI-аналитика запрещена.

### Шаг 6. Drag-and-drop через `LeadStatusService`
- `POST /admin/conversation/{user_id}/stage` (`router.py:776`) → `LeadStatusService.set_status(...,
  source=admin)`; **не** меняет `dialog_owner`; outbox только при реальной смене.
- `BOARD_COLUMNS` → 11 реальных статусов; `silent` — вычисляемый фильтр (не статус).

### Шаг 7. AI-классификация (Trello-независимо)
- Классификатор возвращает `{intent, confidence, evidence, lead_temperature, suggested_status,
  next_action_type, next_action_at}`.
- Детерминированный маппинг intent→status; авто-простановка только разрешённых боту
  (`new/in_progress/info_sent/callback/thinking/invited`) при `confidence ≥ 0.9` и вне
  `manual_status_lock_until`; `invited` → `bot_phase=handoff` + `dialog_owner=manager`.
- `possible_rejection`/`invalid_number` → только рекомендация менеджеру, карточку не двигают.
- LLM: OpenRouter, `anthropic/claude-haiku-4.5` (точный slug), лог токенов/стоимости; при
  выключенном боте не вызывается; FAQ в LLM не отправляется.

### Шаг 8. Trello = stub/outbox-заглушка
- `LeadStatusService` кладёт событие в `outbox` (таблица) при реальной смене `lead_status`.
- Worker — **no-op заглушка** (лог, `trello_sync_status=pending`). Реальный `TrelloCrm`
  (webhook + перемещение + антицикл по `action_id`) — **Phase 2**, по отдельной команде.

---

## 2. Что НЕ входит в Phase 1
- Реальный Trello API (key/token/webhook/перемещение карточек) — Phase 2.
- Фоновая AI-аналитика при перехвате/OFF (отдельный флаг, по умолчанию OFF) — позже.
- Полный перенос LLM-фактов из `knowledge.py` в опубликованную FAQ-базу (если решится F4) — отдельно.
- Удаление `stage`/`intercepted` — после стабилизации.

---

## 3. Список тестов Phase 1

**Модель/совместимость**
- бэкофилл `stage→bot_phase/lead_status`, `intercepted→dialog_owner` корректен;
- запись зеркалится в старые поля; **136 существующих тестов зелёные**.

**Три состояния**
- перехват (`takeover`/ручная отправка/пауза) меняет `dialog_owner`, **не** `lead_status`
  (и **не** создаёт outbox);
- drag-and-drop меняет `lead_status` (создаёт outbox), **не** `dialog_owner`/`bot_phase`;
- `invited` ботом → `lead_status=invited` + `bot_phase=handoff` + `dialog_owner=manager`.

**Рубильники (формула AND)**
- общий OFF → все три бота off, даже если индивидуальный ON (исправление override!);
- общий ON + индивидуальный OFF → выключен только выбранный, остальные работают;
- при OFF: входящее сохраняется; авто-ответ/AI-классификатор/авто-статус/followup не выполняются;
  ручной синк работает; OpenRouter не вызывается.

**LeadStatusService / переходы**
- разрешённый переход применяется; запрещённый — отклоняется;
- `callback_requested→callback`, `will_think→thinking`, `info_delivered→info_sent`,
  `wants_to_come→invited`; ниже `confidence` — без смены;
- менеджер-онли (`tested_thinking/pre_contract/contract/rejected/invalid_number`) бот не ставит;
  `possible_rejection` → рекомендация без движения карточки;
- `next_action_at` парсится/сохраняется при названном времени.

**Конфликты / lock**
- `manual_status_lock_until=30м` активен → бот не меняет `lead_status`, но пишет `suggested_status`;
- два ручных изменения → применяется последнее по времени получения сервером; конфликт в аудит;
- приоритет ручного над авто.

**Outbox-заглушка**
- **реальная** смена `lead_status` создаёт ровно одно outbox-событие `pending`;
- перехват/смена `dialog_owner`/no-op outbox **не** создают;
- worker-заглушка не роняет флоу; локальный статус не зависит от Trello.

**Перехват на лету**
- менеджер перехватил во время генерации → AI-ответ не отправляется (drop).

**Регресс каркаса**
- дедуп вебхуков Wappi, дебаунс/локи, FAQ, валидатор, followup/awaiting/watchdog — зелёные.

# Requirements Traceability — соответствие документации решениям владельца

> Compliance-аудит всей документации `docs/` относительно последних решений владельца проекта.
> Статус отражает состояние **после** правок этого аудита (документы приведены к единой версии).
> Противоречия «документ↔код» помечены и вынесены в Phase 1 (код правится отдельной командой).
>
> **Легенда статусов:** `implemented-in-docs` — зафиксировано и непротиворечиво в доках;
> `partially-documented` — частично; `missing` — не зафиксировано; `contradictory` — есть
> противоречие; `pending-customer-confirmation` — ждёт ответа заказчика.
>
> Документы: `trello-status-sync-spec.md` (TS), `admin-bot-control-and-ai-classification-spec.md`
> (AB), `faq-knowledge-base-spec.md` (FAQ), `phase1-implementation-plan.md` (P1), `HANDOFF.md` (H).

---

## 1. Таблица трассируемости

| ID | Требование | Источник | Где зафиксировано | Статус | Противоречие | Что исправить |
|---|---|---|---|---|---|---|
| **R1.1** | WhatsApp ↔ бот/менеджер ↔ админка+PostgreSQL ↔ Trello; WA напрямую с Trello не связан | Owner §1 / Q1 | TS §0.5, AB §5 «Решения» | implemented-in-docs | — | — |
| **R1.2** | Conversation и Lead — разные сущности; `conversation.lead_id→lead.id` | Owner §2 / Q2 | TS §0.5, AB §5 | implemented-in-docs | Код: одна сущность `ConversationView` (`store.py:36-58`) | Phase 1 §5: разделить |
| **R1.3** | PostgreSQL — источник истины; Trello — внешнее отображение | Owner §1 | TS §0.5, P1 §0 | implemented-in-docs | — | — |
| **R2.1** | Три состояния `lead_status`/`bot_phase`/`dialog_owner` (одно `stage` нельзя) | Owner §2 | AB §6, TS §0.5 | implemented-in-docs | Код: единое `DialogState.stage` (`state.py:25`) | Phase 1 §1: разделить |
| **R2.2** | Перехват меняет `dialog_owner`, но НЕ `lead_status` | Owner §2/§5 | AB §3.1/§6, тесты §18 | implemented-in-docs | Код: авто-хендофф `stage=manager` двигает и стадию, и молчание (`orchestrator.py:209-211`) | Phase 1 §4: развести |
| **R2.3** | Drag-and-drop меняет `lead_status`, но НЕ `dialog_owner` | Owner §2 | AB §6/§11, P1 §6 | implemented-in-docs | — | Phase 1 §6 |
| **R3.1** | `effective = global_bots_enabled AND individual_bot_enabled`; общий OFF отключает все три | Owner §3 | AB §2/§Q11/§18, TS Q11, P1 §0/§5 | implemented-in-docs | **Код: override** (`_bots_on`, `orchestrator.py:87-97`) — индивидуальный ON включает при общем OFF | Phase 1 §5: `_bots_on`→AND |
| **R3.2** | Убрать все тексты «per-bot переопределяет общий / работает при общем OFF» | Owner §3 | AB §2/§18 (переписаны), TS Q11, H §рубильник (помечено) | implemented-in-docs | Ранее противоречило (было «переопределяет») — **исправлено** | — |
| **R4.1** | OFF: входящие сохраняются; менеджер отвечает; ручные статусы; НЕ генерируется AI-ответ; НЕ вызывается классификатор; нет авто-`lead_status`; нет followup; OpenRouter не тратится; фоновая AI-аналитика запрещена | Owner §4 | AB §14/§16/§2.1, P1 §5 | implemented-in-docs | Ранее AB §16 говорил «ПРЕДП. только пассивная аналитика» — **исправлено на запрещена** | — |
| **R5.1** | `dialog_owner=manager`: бот молчит; авто-статусы нет; ручные действия есть; фоновая AI-аналитика — только через будущий флаг, по умолчанию OFF | Owner §5 | AB §3/§14/§16 | implemented-in-docs | Ранее «допускается фоновая AI-аналитика» — **исправлено** | — |
| **R6.1** | LLM возвращает intent/confidence/evidence/suggested_status; НЕ выбирает Trello-колонку; детерминированный `LeadStatusService` применяет только разрешённые переходы | Owner §6 | AB §7/§8/§9, TS §4 | implemented-in-docs | — | — |
| **R7.1** | LLM MVP: OpenRouter; main+cheap `anthropic/claude-haiku-4.5` (точный slug, не latest); лог input/output токенов и стоимости; FAQ не шлём в LLM; при OFF LLM не вызывается; контекст = последние сообщения + summary; `OPENROUTER_API_KEY` только в `.env` | Owner §7 | AB §7-A | implemented-in-docs | Код: `config.py` `llm_model_main/cheap=""` (плейсхолдеры) | Обновить `.env`/конфиг отдельным шагом (в аудите конфиги не трогаем) |
| **R8.1** | Управляемый FAQ из админки: create/edit/enable/disable/delete | Owner §8 | FAQ §2/§6 | implemented-in-docs | Код: delete отсутствует (только toggle) | Phase 2 FAQ |
| **R8.2** | Draft/Published; тест до публикации; без перезапуска | Owner §8 | FAQ §2/§4/§9 | implemented-in-docs | Код: нет `is_published`; тест матчит live | Phase 2 FAQ |
| **R8.3** | Ответы RU и KY; несколько вариантов вопроса | Owner §8 | FAQ §7 | implemented-in-docs | Код: один `answer`; `patterns[]` есть | Phase 2 FAQ: `answer_ru/ky` |
| **R8.4** | Категории и приоритет; `handoff_only`; `valid_from/valid_until` | Owner §8 | FAQ §7 | implemented-in-docs | Код: нет бизнес-категорий/`valid_*` (есть funnel/priority/handoff_only) | Phase 2 FAQ |
| **R8.5** | История версий; автор; откат | Owner §8 | FAQ §7/§9 | implemented-in-docs | Код: нет версий/отката (есть общий аудит) | Phase 2 FAQ |
| **R8.6** | Подтверждение для чувствительных категорий (цена/скидки/сроки/проходной балл/оплата/договор) | Owner §8 | FAQ §5/§9 | implemented-in-docs | Код: нет workflow подтверждения | Phase 2 FAQ |
| **R8.7** | Опубликованный FAQ обрабатывается ДО LLM; LLM получает только опубликованную актуальную базу | Owner §8 | FAQ §3/§4 | implemented-in-docs | Код: FAQ до LLM есть; но LLM берёт факты из хардкода `knowledge.py`, не из базы | Phase 2 FAQ (см. F4) |
| **R9.1** | Phase 1: канонический `lead_status` в Lead/PG; `DialogState` не второй источник истины; `stage`/`intercepted` временно; outbox только при реальной смене `lead_status`; нет реального Trello API; общий рубильник абсолютный приоритет; тесты; 136 зелёные | Owner §9 | P1 §0/§1/§3 | implemented-in-docs | — | — |
| **R10.1** | Q1–Q11 не должны быть одновременно «утверждены» и «открыты» | Owner §10 | AB §19 (переписан), TS §9 (переписан) | implemented-in-docs | Ранее дублировались как открытые — **исправлено** | — |
| **R10.2** | Разделить: решения владельца / реальные вопросы заказчику / доступы Phase 2 | Owner §10 | AB §19.A/§19.B, TS §9 | implemented-in-docs | — | — |

---

## 2. Пункты, ожидающие подтверждения заказчика (pending-customer-confirmation)

| ID | Вопрос | Где | Статус |
|---|---|---|---|
| C1 | Бизнес-смысл `pre_contract` vs `contract` | AB §19.A B1, TS §9 | pending-customer-confirmation |
| C2 | `contract` = финальный успех «поступил/оплатил»? | AB §19.A B2 | pending-customer-confirmation |
| C3 | Значение цветных меток Trello | AB §19.A B3, TS §9 | pending-customer-confirmation |
| C4 | Финальный список чувствительных FAQ-категорий и кто утверждает публикацию | FAQ §11 F1/F2 | pending-customer-confirmation |
| C5 | Порог `confidence`=0.9 (подтвердить); фолбэк при отсутствии `answer_ky` | AB §19.A B5, FAQ F5 | pending-customer-confirmation |

## 3. Технические доступы (только Phase 2, не блокируют Phase 1)

| ID | Доступ | Статус |
|---|---|---|
| A1 | Trello `api_key`/`token`/`board_id` + id 11 списков | pending (Phase 2) |
| A2 | WhatsApp/Wappi token + `wappi_profile_id` + webhook (3 профиля) | pending (Phase 2) |
| A3 | `OPENROUTER_API_KEY` в `.env` | pending |

---

## 4. Противоречия «документ↔код» (для Phase 1, код правится отдельно)

| # | Противоречие | Файл кода | Требование | Действие |
|---|---|---|---|---|
| X1 | Рубильник — **override**, а требуется **AND** | `orchestrator.py:87-97` (`_bots_on`) | R3.1 | Phase 1 §5 |
| X2 | Единое `stage` вместо трёх состояний | `state.py:25`, `leadstate.py:12-19` | R2.1 | Phase 1 §1 |
| X3 | Авто-хендофф двигает стадию и молчание одним полем | `orchestrator.py:209-211` | R2.2 | Phase 1 §4 |
| X4 | Одна сущность вместо Conversation+Lead | `panel/store.py:36-58` | R1.2 | Phase 1 §5 |
| X5 | 7 колонок ≠ 11 статусов Trello | `admin/router.py:38-46`, `leadstate.py:12-19` | R2.x | Phase 1 §6 |
| X6 | Модели LLM пустые | `config.py` (`llm_model_main/cheap`) | R7.1 | конфиг-шаг (не в аудите) |
| X7 | FAQ: нет published/version/rollback/ru-ky/valid_*/delete/категорий/подтверждения | `core/faq.py`, `crm/db.py`, `admin/router.py` | R8.x | Phase 2 FAQ |

> Все X-пункты — задачи **кода**, не документации. В этом аудите код не трогается.

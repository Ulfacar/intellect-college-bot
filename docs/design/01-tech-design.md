# Технический дизайн: воронка `admission` (Intellect IT & Business College)

Роль документа — детальный дизайн по `docs/plan-college-bot-mvp.md` (задачи T1–T8).
Проектируется ПОД существующий каркас: `runner.run_turn` + `FunnelSpec`, `funnels/base.py`,
`CRMPort`, `leadstate.py`, `core/faq.py` — эти механизмы НЕ меняются, меняется только
доменное наполнение. Codex реализует по этому документу.

Обозначения: «инструкция модели» = строка, которую `exec_tool` возвращает в `tool_result`.
Это НЕ текст клиенту — это указание LLM, что и как сказать (паттерн из `_tours_exec_tool`).

---

## 1. `app/funnels/admission.py` — AdmissionFunnel

Единственная воронка приложения (по образцу `ToursFunnel`, но без внешних интеграций —
у колледжа нет live-поиска).

```
REQUIRED_FIELDS = ["name", "grade_base", "direction"]
```

| Поле | Смысл | Примеры значений |
|---|---|---|
| `name` | Имя абитуриента (или родителя, если пишет родитель) | «Айбек» |
| `grade_base` | База: после какого класса поступает | «9» / «11» |
| `direction` | Интересующее направление (одно из 8 или «не определился») | «Программная инженерия и ИИ» |

Контакт (телефон) приходит из WhatsApp — в REQUIRED_FIELDS не входит.
Поля из блока 6 опросника «год рождения», «откуда узнал» — НЕ в MVP (⚠ в опроснике,
уточнить у поддержки; если подтвердят — добавляются как необязательные, бот спрашивает
их только перед хендоффом, не блокируя диалог).

Сигнатуры (в стиле `ToursFunnel`):

```
class AdmissionFunnel:
    name = "admission"

    async def handle(self, msg: Message, state: DialogState) -> str | None:
        # 1) llm_enabled() → app.agent.runner.run_admission_turn(state, msg.text)
        # 2) fallback без LLM-ключа: collect_qualification(state, msg.text,
        #    REQUIRED_FIELDS, _ask_for); когда всё собрано — create_lead (если нет
        #    deal_id), stage="manager", crm.update_stage("manager_handoff"),
        #    вернуть текст передачи менеджеру (см. 02-dialog-ux §5.4 — реплика хендоффа).
```

Fallback-вопросы `_ask_for(field)` (нужны и для `core/faq.qualification_question`):

- `name` → «Как я могу к вам обращаться? 😊»
- `grade_base` → «Подскажите, вы поступаете после 9 или после 11 класса?»
- `direction` → «Какое направление вам интересно? У нас IT и бизнес-направления, могу коротко рассказать.»

`app/funnels/__init__.py`: реестр сокращается до `{"admission": AdmissionFunnel()}`.

`DialogState` (`core/state.py`) не меняется структурно; только комментарий поля
`funnel: str | None = None  # admission` (сейчас «tours | visa | tickets»).

---

## 2. `app/agent/runner.py` — ADMISSION_SPEC и `_admission_exec_tool`

Из runner.py удаляются: импорты tourvisor/visa_pricing/visa-funnel/tickets, `_tourvisor`,
`_tours_exec_tool`, `_visa_exec_tool`, `_tickets_exec_tool`, TOURS/VISA/TICKETS_SPEC и их
`run_*_turn`. Общий `run_turn` (цикл, валидатор, intercepted, MAX_TOOL_ITERATIONS) — без изменений.

```
from app.agent.prompts.admission import SYSTEM as ADMISSION_SYSTEM, system_for_manager

ADMISSION_SPEC = FunnelSpec(
    name="admission",
    system=ADMISSION_SYSTEM,
    tools=tools_for(["ask_qualification", "handoff_to_manager",
                     "escalate_to_office", "crm_update_stage"]),
    exec_tool=_admission_exec_tool,
)

async def run_admission_turn(state: DialogState, user_text: str) -> str | None:
    spec = ADMISSION_SPEC
    if state.manager_name:  # персона под конкретный номер (3 номера = один мозг)
        spec = replace(ADMISSION_SPEC, system=system_for_manager(state.manager_name))
    return await run_turn(state, user_text, spec)
```

### 2.1 Поведение `_admission_exec_tool(name, args, state, crm)`

Общий принцип каркаса сохраняем: **tool_result — инструкция модели, а не факт клиенту**;
никаких «менеджер уже ждёт», пока данные не собраны.

**`ask_qualification`** — модель фиксирует собранные данные и следующий вопрос.

```
1. state.qualification.update({k: v for k, v in args.items()
                               if k in {"name", "grade_base", "direction"} and v})
2. если появились данные и state.deal_id пуст →
   state.deal_id = await crm.create_lead({"user_id": state.user_id}, "admission",
                                         state.qualification)
3. state.stage = "qualification"; state.pending_field = args.get("field") or None
4. missing = [f for f in REQUIRED_FIELDS if f not in state.qualification]
```

Возвращаемые инструкции:

- если `missing` пуст:
  «Все поля квалификации собраны (имя, база, направление). Больше анкетных вопросов
  НЕ задавай. Ответь на текущий вопрос клиента, если он есть, и предложи следующий шаг:
  запись на вступительный тест (escalate_to_office) или передачу менеджеру.»
- иначе:
  «Записал. Ещё не собрано: {missing}. Задай ОДИН вопрос про «{args['field']}» —
  сначала коротко ответь на вопрос клиента, если он его задал. Не спрашивай два поля сразу.»

**`escalate_to_office`** — в домене колледжа означает «пригласить на вступительный тест /
в колледж» (семантика меняется, имя инструмента сохраняем — меньше правок каркаса; в
описании tools это фиксируется явно). Стадия → `test_invite`.

Страховочные ветки по образцу tours (без имени визит не подтверждаем):

```
1. state.qualification.update(...)  # name, grade_base, direction, visit_time если пришли
2. client_name = args.get("name") or state.qualification.get("name")
3. если client_name пуст → вернуть инструкцию:
   «Клиент готов на тест, но имя не собрано. НЕ подтверждай запись. Сначала ответь на
   текущий вопрос клиента, затем спроси, как к нему обращаться.»
4. если state.deal_id пуст → crm.create_lead(...)
5. crm.update_stage(state.deal_id, "test_invite"); state.stage = "test_invite"
6. вернуть инструкцию:
   «Зафиксировано: клиент приглашён на вступительный тест. Скажи коротко: заявку на тест
   передал менеджеру приёмной, он свяжется в этом чате и подтвердит дату, время и формат.
   НЕ называй сам дату/время/формат теста и проходной балл — порядок записи подтверждает
   менеджер. Напомни, что тест по математике и английскому, длительность 1,5 часа, и что
   персональная скидка обсуждается после теста (размер не называй).»
```

Примечание: в опроснике формат теста (онлайн/очно, 3.4) и способ записи (3.5) — ⚠ не
заполнены, поэтому инструмент фиксирует НАМЕРЕНИЕ, а подтверждение записи — за менеджером.
Когда поддержка заполнит 3.4/3.5, инструкция шага 6 обновляется фактами.

**`handoff_to_manager`** — передача живому менеджеру (все триггеры из плана §5).

```
1. args.get("reason") → добавить в state.qualification["escalation_reason"] (для карточки)
2. если state.deal_id пуст и есть хоть какие-то данные → crm.create_lead(...)
3. если state.deal_id → crm.update_stage(state.deal_id, "manager_handoff")
4. state.stage = "manager"
5. вернуть: «Передано менеджеру приёмной комиссии. Скажи клиенту КОРОТКО и честно:
   вопрос передал менеджеру, он ответит в этом чате; НЕ утверждай, что менеджер уже
   онлайн, и НЕ отвечай сам на вопрос, из-за которого эскалируешь.»
```

**`crm_update_stage`** — редкий явный сдвиг стадии моделью (напр. клиент передумал:
«пока просто узнаю» после приглашения на тест).

```
1. stage = args["stage"]; допустимы ТОЛЬКО {"qualification", "consulting", "test_invite"}
   (manager_handoff — только через handoff_to_manager; won/lost бот не ставит никогда)
2. вне списка → вернуть «Недопустимая стадия, ничего не менял.» (без падения)
3. state.stage = stage; если state.deal_id → crm.update_stage(state.deal_id, stage)
4. вернуть «Стадия обновлена: {stage}. Продолжай диалог.»
```

Неизвестный инструмент → `return "ok"` (как в каркасе).

---

## 3. `app/agent/tools.py` — схемы инструментов

**Удалить:** `search_tours`, `score_visa`, `submit_request` (билеты не нужны — заявка
абитуриента фиксируется через ask_qualification/escalate; отдельный submit_request был бы
дублем handoff).

**Оставить/переписать** (итоговый `TOOLS` — ровно 4 инструмента):

```
{
  "name": "ask_qualification",
  "description": "Зафиксировать собранные данные абитуриента и задать следующий "
                 "вопрос квалификации. Вызывай, как только клиент сообщил имя, базу "
                 "(9/11 класс) или направление — даже вперемешку с другим вопросом.",
  "input_schema": {
    "type": "object",
    "properties": {
      "field":      {"type": "string", "enum": ["name", "grade_base", "direction"],
                     "description": "Какое поле собираешься спросить следующим."},
      "question":   {"type": "string", "description": "Формулировка вопроса клиенту."},
      "name":       {"type": "string", "description": "Имя, если клиент уже назвал."},
      "grade_base": {"type": "string", "enum": ["9", "11"],
                     "description": "База поступления, если уже известна."},
      "direction":  {"type": "string",
                     "description": "Интересующее направление из списка колледжа, "
                                    "либо «не определился»."}
    },
    "required": ["field", "question"]
  }
},
{
  "name": "escalate_to_office",
  "description": "Пригласить абитуриента на вступительный тест / в колледж: клиент "
                 "готов записаться или прийти. Фиксирует намерение; дату и формат "
                 "подтверждает менеджер.",
  "input_schema": {
    "type": "object",
    "properties": {
      "reason":     {"type": "string"},
      "name":       {"type": "string", "description": "Имя клиента, если названо."},
      "grade_base": {"type": "string", "enum": ["9", "11"]},
      "direction":  {"type": "string"},
      "visit_time": {"type": "string",
                     "description": "Когда клиенту удобно (если сам сказал)."}
    },
    "required": ["reason"]
  }
},
{
  "name": "handoff_to_manager",
  "description": "Передать диалог живому менеджеру приёмной: вопрос вне базы знаний, "
                 "торг о цене/скидке, оплата/договор, жалоба, просьба человека, "
                 "неуверенность, 2 хода без осмысленного ответа.",
  "input_schema": {
    "type": "object",
    "properties": {"reason": {"type": "string",
                              "description": "Краткая причина — попадёт в карточку."}}
  }
},
{
  "name": "crm_update_stage",
  "description": "Сдвинуть карточку абитуриента по канбану (только стадии бота).",
  "input_schema": {
    "type": "object",
    "properties": {"stage": {"type": "string",
                             "enum": ["qualification", "consulting", "test_invite"]}},
    "required": ["stage"]
  }
}
```

`tools_for()` — без изменений.

---

## 4. Стадии диалога и `STAGE_TO_COLUMN`

### 4.1 Стадии бота (значения `state.stage` / аргумент `crm.update_stage`)

| Стадия | Кто ставит | Смысл |
|---|---|---|
| `greeting` | дефолт DialogState | Новый лид, диалог только начался |
| `qualification` | ask_qualification | Бот собирает имя/базу/направление |
| `consulting` | crm_update_stage | Квалификация собрана, бот консультирует по whitelist |
| `test_invite` | escalate_to_office | Клиент приглашён на вступительный тест (горячий) |
| `manager` / `manager_handoff` | handoff_to_manager | Диалог у живого менеджера |
| `follow_up` | scheduler (каркас) | Автодожим молчащего |

Исходы `won` / `lost` — только руками менеджера в админке (кнопки «Исход»), бот не ставит.

### 4.2 `leadstate.STAGE_TO_COLUMN` — правка

Ключи колонок каркаса СОХРАНЯЕМ (`greeting/qualification/progress/office/manager/follow_up`)
— на них завязаны CSS доски (`.col[data-col=...]`), `SILENT_EXCLUDED_COLUMNS`, drag-and-drop
(`/conversation/{id}/stage` шлёт key колонки). Меняются только ярлыки в админке (см. 03-admin-ui)
и маппинг стадий:

```
STAGE_TO_COLUMN = {
    "greeting": "greeting", "new": "greeting",
    "qualification": "qualification",
    "consulting": "progress", "progress": "progress",
    "test_invite": "office", "office": "office", "office_consultation": "office",
    "manager": "manager", "manager_handoff": "manager",
    "follow_up": "follow_up", "followup": "follow_up", "callback": "follow_up",
}
```

(удаляются туристические синонимы `scoring/search/visa_scoring`; `office`/`office_consultation`
остаются как легаси-синонимы — их может прислать drag-and-drop доски.)

`HUMAN_STAGES` дополнить: `{"office", "office_consultation", "test_invite", "manager",
"manager_handoff"}` — приглашённого на тест автодожим не пингует (лидом занимается менеджер).
`NOISE_STAGES`, `SILENT_EXCLUDED_COLUMNS`, `TERMINAL_OUTCOMES` — без изменений.

### 4.3 Человеческие ярлыки колонок (канбан приёмной)

| key | Ярлык |
|---|---|
| greeting | Новые лиды |
| qualification | Квалификация |
| progress | Консультация |
| office | Приглашён на тест |
| manager | У менеджера |
| follow_up | Автодожим |

Исходы: `won` → «Поступает», `lost` → «Слив». (Блок 8 опросника пуст — колонки предложены
нами, подтвердить у Emir/поддержки.)

---

## 5. `CollegeAdminCrm(CRMPort)` — Фаза 2, контракт закладываем сейчас

Файл `app/integrations/crm/college_admin.py`, по образцу `bitrix24.py`. Формат вебхуков
админки Emir неизвестен → адаптер проектируем абстрактно, все URL/поля — TODO под реальный
контракт. До получения контракта работаем на `CRM_BACKEND=stub|postgres` (MVP), фабрика
`get_crm()` получает ветку `"college_admin"`.

```
class CollegeAdminCrm:
    """CRMPort → вебхуки админки колледжа. TODO: подтвердить контракт у Emir."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._base = settings.college_admin_webhook_url.rstrip("/")   # env COLLEGE_ADMIN_WEBHOOK_URL
        self._key = settings.college_admin_api_key                    # env COLLEGE_ADMIN_API_KEY
        self._client = client  # инъекция для тестов, иначе httpx.AsyncClient(timeout=20)

    async def _post(self, path: str, payload: dict) -> dict:
        # POST {base}{path}, заголовок Authorization: Bearer {key} (TODO: схема авторизации).
        # raise_for_status; вернуть resp.json(). Владение клиентом — как в Bitrix24Crm.

    async def create_lead(self, contact, funnel, data) -> str:
        # TODO контракт. Черновик: POST /leads
        # {"source": "whatsapp_bot", "phone": contact["user_id"], "funnel": funnel,
        #  "name": data.get("name"), "grade_base": data.get("grade_base"),
        #  "direction": data.get("direction"), "raw": data}
        # ответ: {"id": "..."} → вернуть str(id).
        # Деградация: base не задан → logger.warning, вернуть "" (бот не падает).

    async def update_stage(self, deal_id, stage) -> None:
        # stage_id = settings.college_stage_map.get(stage)  # карта наших стадий → стадий админки
        # нет маппинга или deal_id пуст → warning + пропуск (страховка как в Bitrix).
        # TODO черновик: POST /leads/{deal_id}/stage {"stage": stage_id}

    async def add_note(self, deal_id, text) -> None:
        # TODO черновик: POST /leads/{deal_id}/notes {"text": text}

    async def send_message(self, chat_id, text) -> None:
        # НЕ нужен: отправка идёт через каналы (wappi/telegram), не через админку.
        # Оставить no-op с warning — метод обязателен по CRMPort.
```

Карта `settings.college_stage_map: dict[str, str]` — стадии бота (§4.1) → идентификаторы
стадий админки; пустая карта = мягкая деградация. `_format_qualification` из bitrix24
переиспользовать (вынести в общий helper или скопировать — на усмотрение Codex).

Что запросить у Emir для реализации (TODO-чеклист в докстринге адаптера):
базовый URL; авторизация (header?); эндпоинт/тело создания лида и формат id в ответе;
эндпоинт смены стадии + список стадий админки; эндпоинт заметок; поведение при дубле
(тот же телефон дважды) — upsert или новый лид.

---

## 6. `app/agent/validator.py` — детекторы под колледж

Каркас (strip_markdown, MAX_LEN, multiple_questions, сигнатура `validate_reply(text, funnel)`)
сохраняется. Туристические ветки (`tours_price_disclaimer_added`, `price_in_no_price_funnel`,
`possible_visa_guarantee`, `PRICE_DISCLAIMER`) — удалить вместе с T6.

Философия каркаса не меняется: **авто-чиним только форму** (markdown), **факты не
редактируем** — рискованное логируем через `observ.note_validation` (видно на /admin/system),
профилактика — в промпте. Автозамена факта могла бы сама породить враньё.

Новые детекторы (все — ТОЛЬКО лог, `funnel == "admission"` кроме markdown):

```
# 1) Гарантия поступления/зачисления/гранта/скидки (ru + ky).
#    Отрицания «не гарантируем» отсекаем negative lookbehind, как в _GUARANTEE каркаса.
_ADMISSION_GUARANTEE = re.compile(
    r"(100\s?%|(?<!не )гаранти\w*|кепилдик\w*"
    r"|точно (?:поступ|зачисл|пройд[её]те|получите грант)"
    r"|обязательно (?:поступ|зачисл|пройд[её]те)"
    r"|(?:грант|скидк\w+|зачислени\w+) (?:обеспечен|гарантирован)\w*)",
    re.IGNORECASE)
→ violation "admission_guarantee"

# 2) Сумма, отличная от $6500 (единственная разрешённая цифра стоимости).
#    Берём _PRICE каркаса; из найденных сумм нормализуем числа (убрать пробелы/разделители)
#    и помечаем, если есть число >= 100, не равное 6500 (мелкие числа — баллы/часы — не цены;
#    отсечка по контексту валюты уже в _PRICE).
→ violation "admission_price_mismatch"

# 3) Размер скидки в цифрах (скидку бот не называет: «после теста»).
_DISCOUNT_AMOUNT = re.compile(r"(скидк\w*|арзандат\w*)[^.\n]{0,40}?\d+\s?%|"
                              r"\d+\s?%[^.\n]{0,25}(скидк|арзандат)", re.IGNORECASE)
→ violation "admission_discount_amount"

# 4) Проходной балл с цифрой (спорный факт «90+» — бот не утверждает).
_PASSING_SCORE = re.compile(r"(проходн\w+ балл|өтүү балл\w*)[^.\n]{0,30}\d+|"
                            r"\d+\s?балл\w*[^.\n]{0,30}(проходн|өтүү|порог)", re.IGNORECASE)
→ violation "admission_passing_score"

# 5) Спорный срок «3 года» (расхождение автоприветствия с 2г10м/1г10м).
_DURATION_3Y = re.compile(r"\b(3|три|үч)\s*(год|года|жыл)", re.IGNORECASE)
→ violation "admission_duration_claim"
```

Сводная таблица «чинить vs логировать»:

| Сигнал | Действие |
|---|---|
| markdown-разметка | авто-чинится (strip_markdown), любая воронка |
| гарантия поступления/гранта | лог `admission_guarantee` |
| цена ≠ $6500 | лог `admission_price_mismatch` |
| размер скидки в % | лог `admission_discount_amount` |
| проходной балл с цифрой | лог `admission_passing_score` |
| срок «3 года» | лог `admission_duration_claim` |
| длина > 600 | лог `too_long` (каркас) |
| >1 вопроса | лог `multiple_questions` (каркас) |

Обоснование «не чинить»: детекторы срабатывают и на честные фразы («поступление не
гарантируем», «скидку назовёт менеджер после теста, её размер не назову») — automatch-правка
испортила бы корректный ответ. Если по метрикам /admin/system сработки станут частыми —
отдельным решением вводить блокирующий режим (замена реплики на эскалацию), в MVP не делаем.

---

## 7. Сопутствующие правки (для полноты картины Codex)

- `core/router.py`: `detect_funnel()` всегда возвращает `"admission"`; KEYWORDS удалить.
- `core/faq.py`: `VALID_FUNNELS = {"admission"}`; `qualification_question` — ветка
  admission → `app.funnels.admission._ask_for`; `seed_defaults` — новые правила из
  whitelist (§4 плана): часы работы (⚠ плейсхолдер), адрес (Ибраимова 103/1А), дедлайн
  12.08, документы, направления, тест (предметы/1,5 ч), цена $6500, скидка-после-теста,
  «гарантируете поступление?» → честный ответ без гарантий. Кыргызские паттерны в каждом
  правиле (`канча турат`, `кайда жайгашкан`, `качан чейин`, `кандай багыттар`, `документ`,
  `тест кандай` и т.п.) — формулировки ответов берутся из 02-dialog-ux.
- `core/branding.py`: полная замена констант (T1): `COLLEGE_NAME`, `COLLEGE_ADDRESS`
  («г. Бишкек, ул. Ибраимова 103/1А, B Block»), `COLLEGE_WORKING_HOURS` (⚠ плейсхолдер
  «уточнить у поддержки»), `ADMISSION_DEADLINE` («до 12 августа»), `TUITION_PRICE`
  («$6500, контракт» — период год/курс ⚠), `DIRECTIONS` (8 направлений), `TEST_FACTS`
  (математика + английский, 1,5 часа), `DOCUMENTS`, `DISCOUNT_POLICY` («размер скидки
  озвучивается после теста — цифру не называем»), `QUICK_REPLIES["admission"]` и
  `FOLLOWUP_PINGS["admission"]` (тексты — в 02-dialog-ux §6), `wait_ack_for`.
- `agent/prompts/admission.py` + `agent/prompts/knowledge.py` (`ADMISSION_FAQ`,
  `STOP_WORDS_AND_HOURS` под колледж) — содержимое промпта строится из 02-dialog-ux;
  структура файла — как `prompts/tours.py` (SYSTEM + LANGUAGE_AND_ESCALATION + FAQ + STOP),
  `system_for_manager(manager_name)` сохраняется (3 номера, персоны на номер).
- `agent/prompts/common.py`: `LANGUAGE_AND_ESCALATION` — заменить упоминания Frunze/туров
  на колледж; правило идентичности — по 02-dialog-ux §1; блок «язык ru/ky», «1 вопрос за
  ход», «2 бессмысленных хода → handoff» сохраняются дословно (это ядро каркаса).
- `state.py::RedisStateStore.KEY_PREFIX` — оставить как есть (не ломать совместимость)
  либо сменить на `college:dialog:` одним решением с чистым деплоем; рекомендация — сменить,
  прод-состояния frunze не переносятся.

---

## 8. Открытые вопросы для Opus/Emir

1. **Контракт вебхуков админки** (блокер Фазы 2, §5): URL, авторизация, эндпоинты
   create/stage/note, список стадий канбана админки, поведение при дубле телефона.
2. **$6500 — за год или за весь курс** (опросник 2.1)? Пока бот говорит «стоимость по
   контракту — 6500 долларов» без периода; после ответа — уточнить формулировку в branding.
3. **Колонки канбана** (§4.3) предложены нами — Блок 8 опросника пуст. Подтвердить ярлыки
   и исходы «Поступает/Слив».
4. **Формат и запись на тест** (3.4/3.5): онлайн/очно, как записывают. Сейчас
   escalate_to_office фиксирует намерение, запись подтверждает менеджер.
5. **Нужны ли поля «год рождения» и «откуда узнал»** в квалификации (Блок 6 ⚠)?
6. **Часы работы приёмной** (0.3) — для `COLLEGE_WORKING_HOURS` и текстов wait_ack.
7. **Модель OpenRouter** (T7) — плейсхолдер, ждём Emir.
8. Допустимо ли переименовать префикс Redis (`frunze:dialog:` → `college:dialog:`) —
   зависит от того, деплоится ли форк на чистое окружение (предполагаем да).

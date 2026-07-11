# План миграции: frunze-travel-bot → intellect-college-bot

Каркас переиспользуем как есть (~70-80%). Домен турагентства локализован в нескольких местах —
их вырезаем и заменяем на домен колледжа.

## НЕ трогаем (доменно-нейтральный каркас)

`app/main.py`, `app/core/orchestrator.py`, `scheduler.py`, `state.py`, `observ.py`, `flags.py`,
`app/agent/llm.py` (generic OpenRouter-адаптер), каркас `runner.py` (`run_turn` + `FunnelSpec`),
`app/channels/*` (wappi/telegram/outbound/base), `core/bots.py`, `integrations/crm/{port,stub,postgres,db}.py`,
`integrations/panel/store.py`, `app/admin/*`, `core/faq.py`, `leadstate.py`, followup/awaiting/watchdog (механика).

## Заменяем (домен колледжа)

| # | Что | Файлы | Действие |
|---|---|---|---|
| 1 | Бренд-константы | `app/core/branding.py` | Название, адрес, часы, факты ($6500, тест 1.5ч, дедлайн 12.08), тексты followup-пингов, quick replies |
| 2 | База знаний / FAQ | `app/agent/prompts/knowledge.py`, `core/faq.py` seed_defaults | Факты колледжа (из `college-domain-facts` + опросник) |
| 3 | Промпты воронки | `app/agent/prompts/*` (кроме `common.py`) | 1 воронка `admission` вместо tours/visa/tickets |
| 4 | Воронки | `app/funnels/*` → `funnels/admission.py` | Квалификация абитуриента, tools/exec_tool в `runner.py` |
| 5 | Роутер | `app/core/router.py` | KEYWORDS не нужны (один сценарий, `bot.scenario="admission"`) |
| 6 | Валидатор-детекторы | `app/agent/validator.py` | Запреты: не обещать гарантию поступления/грант, не выдумывать баллы/стоимость/сроки |
| 7 | CRM-адаптер | `integrations/crm/bitrix24.py` → `college_admin.py` | POST на вебхуки админки Emir (образец = bitrix24.py) |
| 8 | Удалить турагентство | `app/integrations/tourvisor/`, `funnels/{tours,visa,tickets}.py`, `prompts/{tours,visa}.py` | Вырезать |
| 9 | Env/реестр ботов | `.env`, `BOTS` | 3 номера, scenario=admission |
| 10 | Модель | `LLM_MODEL_MAIN` | Актуальная модель (frunze стоял на устаревшем claude-3.5-sonnet) |

## Блокеры (нужно от Emir)
- Референс вебхуков админки колледжа → шаг 7
- Какая модель OpenRouter → шаг 10
- Доступы WhatsApp-провайдера (наши) → запуск
- Заполненный опросник → шаги 2, 3

## Порядок
Ветка `main` = рабочий форк-каркас (baseline). Далее фиче-ветки по шагам 1→10.
Разблокированное сейчас (не ждёт Emir): шаги 1, 2, 3, 4, 5, 6, 8.

# Intellect College Bot

WhatsApp-бот поддержки/приёмной комиссии колледжа **Intellect IT & Business College** (Бишкек).
Консультирует родителей абитуриентов 24/7, двигает заявки по канбану админки, уведомляет менеджеров,
автодожимает долго думающих. Работает на **wappi (WhatsApp) + OpenRouter (LLM)**.

> Каркас форкнут из внутреннего проекта `frunze-travel-bot` (тот же стек: FastAPI + OpenRouter +
> port/adapter интеграции + автодожим + антигаллюцинационный валидатор). Домен турагентства
> вырезается и заменяется на домен колледжа. См. `docs/migration-plan.md`.

## Железное правило

**Бот НИКОГДА не выдумывает.** Отвечает только из утверждённой базы знаний (FAQ/промпт как whitelist).
Не знает ответ, вопрос вне базы, торг о цене, оплата, жалоба, клиент просит человека → **мгновенная
эскалация на живого менеджера**. Защита реализована на двух уровнях: (1) промпт + FAQ-whitelist,
(2) пост-валидатор `app/agent/validator.py`, ловящий запрещённые фразы после генерации.

## Архитектура (кратко)

```
WhatsApp (wappi) ─webhook→ FastAPI (app/main.py)
   → Orchestrator (дебаунс, локи, лог, рубильник, перехват)
   → FAQ-матчер (детерминированный, до LLM)
   → Funnel "admission" → LLM-агент (OpenRouter, tool-loop) → Validator
   → CRM-порт (заявка/статус в админку колледжа) + Панель (канбан/чат)
Фоновые джобы: followup (автодожим) · awaiting (клиент ждёт человека) · watchdog (здоровье)
```

- **3 бота = 3 номера, один мозг.** Реестр в env `BOTS`, маршрутизация по `wappi_profile_id`.
- **Языки:** авто-детект ru/ky, ответ на языке клиента.
- **CRM:** интерфейс `CRMPort` (`app/integrations/crm/port.py`); адаптер админки колледжа — через вебхуки.

## Запуск (дев)

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env    # заполнить OPENROUTER_API_KEY и т.д.
uvicorn app.main:app --reload
```

Прод — `docker-compose.yml` (app + postgres + redis).

## Что нужно для запуска MVP (блокеры)

1. Доступы WhatsApp-провайдера (**наши**): token + profile_id + право на webhook.
2. `OPENROUTER_API_KEY` + выбранная модель.
3. Референс вебхуков админки колледжа (для CRM-адаптера) — от Emir.
4. Заполненный опросник поддержки → база знаний (`docs/опросник-для-поддержки.md`).

## Документация

- `docs/опросник-для-поддержки.md` — опросник для наполнения базы знаний.
- `docs/migration-plan.md` — план адаптации каркаса под колледж.

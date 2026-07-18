# STAGING 1 — журнал развёртывания (execution log)

Фактическое развёртывание supervised-стенда Intellect College на VPS. **Не production, не 8C/8D.**
Секретов в этом файле НЕТ (только SET / NOT SET и маскированные факты). Реальные секреты живут
только в `/etc/intellect/staging.env` на сервере (chmod 600).

- **Deployment commit:** `2d246127b4019b5e1669855c720d700ad832e63c` (`chore: add secure staging deployment stack`)
- **VPS:** `109.123.249.163` (Ubuntu 24.04.4 LTS, x86_64)
- **Домен (план):** `intellectsuport.duckdns.org`
- **Ветка:** `feature/telegram-pilot` (main не менялся)

---

## 1. Аудит окружения (read-only)

| Проверка | Результат |
|---|---|
| ОС | Ubuntu 24.04.4 LTS, kernel 6.8, x86_64 |
| RAM / диск | 7.8 GiB (6.9 avail) · 145G диск (134G свободно) |
| Порты 80/443/8000/5432/6379 | все **свободны** |
| Reverse proxy | нет активного (nginx/caddy/apache/traefik/haproxy — inactive) |
| Docker / Compose | **отсутствовали** → установлены |
| Существующие проекты | `/opt/emir_bot`, `/opt/openclaw` — **не тронуты** |

## 2. Установка и размещение

- Установлены **Docker CE 29.6.2** + **Compose v5.3.1** (официальный `get.docker.com`; Compose ≫ 2.24.4 — поддержка `!reset`/`!override`).
- Репозиторий склонирован в `/opt/intellect-college`, checkout **строго** на `2d24612…` (подтверждён `git rev-parse HEAD`).

## 3. Секреты и конфигурация

- Каталоги: `/etc/intellect` (700), `/var/backups/intellect` (700).
- `/etc/intellect/staging.env` (**chmod 600**). Сгенерированы **на сервере**: `POSTGRES_PASSWORD`, `SESSION_SECRET`, `ADMIN_PASSWORD`, `ADMIN_BASIC_HASH` (bcrypt через `caddy:2.11.4 hash-password`), Telegram `webhook_secret`.
- Внешние секреты введены владельцем и записаны только в `staging.env`: `OPENROUTER_API_KEY` (**SET**), Telegram bot token (**SET**).
- Доступы владельца (логины/пароли панели и Basic Auth) — в `/etc/intellect/access.txt` (**chmod 600**, читается на сервере, в git/чат/логи не попадают).

Ключевые значения (не секретные):
```
ENVIRONMENT=staging   ADMIN_UI_V2=true   DEMO_LOGIN=false
PANEL_BACKEND=postgres CRM_BACKEND=postgres STATE_BACKEND=redis
LLM_MODEL_MAIN=anthropic/claude-haiku-4.5   LLM_DAILY_BUDGET_USD=1   LLM_MONTHLY_BUDGET_USD=10
Telegram bot: id=college_test_1  title="Intellect College Test"  scenario=admission
TELEGRAM_FEEDBACK_ENABLED=true
```

## 4. Проверки перед запуском

- **`caddy validate` → `Valid configuration`** (реальный запуск, образ `caddy:2.11.4`).
- **`docker compose config`** — контур портов подтверждён: наружу только **Caddy 80/443**; **app/db/redis без публичных портов** (внутренняя сеть).

## 5. Запуск стека

- `docker compose -f docker-compose.yml -f docker-compose.staging.yml up -d --build`.
- Итог: **app healthy · db healthy · redis healthy · caddy up (0.0.0.0:80, 0.0.0.0:443)**.
- **`app /health/ready → 200 {"status":"ready"}`** (внутри контейнера) — БД, миграции (`init_db`) и staging fail-fast валидация прошли.
- `109.123.249.163:80` доступен снаружи (Caddy отдаёт `308` redirect на HTTPS).
- `restart: unless-stopped` — стек переживает перезагрузку VPS.

## 6. Проблемы, найденные и устранённые в ходе развёртывания

1. **`TELEGRAM_ALLOWED_USER_IDS=` (пустая строка) роняет старт app** — pydantic-settings пытается JSON-декодировать пустое значение сложного поля (`SettingsError`). **Решение:** использовать валидный JSON — пустой список `[]`, а при добавлении ID — `[123,456]`. На сервере исправлено на `[]`; рекомендуемый follow-up — то же значение по умолчанию в `.env.staging.example`.
2. **bcrypt-хеш в env-файле нельзя `source`-ить в bash** — `$` в `$2a$14$…` раскрывается как переменные. **Решение:** compose-переменные Caddy извлекать литерально (`grep … | cut -d= -f2-`), НЕ через `source`.
3. **Cosmetic:** docker compose при чтении env_file предупреждает о «переменной» внутри bcrypt-хеша — на функциональность не влияет (хеш нужен только Caddy и передаётся ему корректно; используемые app значения `$` не содержат).

## 7. Telegram-бот

- Токен проверен через `getMe` (токен не логируется): бот **@SupIntellect_bot**, id `8639963338`.
- Ссылка для сбора ID: **https://t.me/SupIntellect_bot** (webhook намеренно НЕ установлен — сбор ID через `getUpdates`).

## 8. Текущий блокер — DNS

`intellectsuport.duckdns.org` резолвится в **`212.112.119.118`** (чужой IP), а не в `109.123.249.163`.
Поэтому ACME (Let's Encrypt/ZeroSSL) не проходит HTTP-01 → **TLS-сертификат не выпущен, публичный HTTPS ещё не работает**.
Сам VPS на `:80` снаружи доступен — как только запись DuckDNS `intellectsuport` укажет на `109.123.249.163`
(через кабинет DuckDNS или обновление по токену), Caddy выпустит сертификат автоматически.

## 9. Осталось до рабочей ссылки

1. Исправить DNS `intellectsuport → 109.123.249.163` (действие владельца/токен).
2. Проверить выпуск сертификата и `https://intellectsuport.duckdns.org/health/ready` + ADMIN UI v2 по HTTPS.
3. Владелец/саппорты жмут `/start` → `getUpdates` → показать имя/username/ID → после подтверждения внести в `TELEGRAM_ALLOWED_USER_IDS` (формат `[id1,id2]`) → перезапустить app.
4. `setWebhook` на `https://intellectsuport.duckdns.org/webhook/telegram/college_test_1` (+ `secret_token`) → `getWebhookInfo`.
5. Smoke-тест: OpenRouter-ответ, расход, диалог в ADMIN UI v2, ручной ответ, takeover/release, Global OFF, персистентность после рестарта, HTTPS без предупреждений, отсутствие секретов в логах.

## 10. Гарантии по секретам

- В git / коде / документации / логах секретов нет — только в `/etc/intellect/staging.env` (600) и `/etc/intellect/access.txt` (600) на сервере.
- `.gitignore`/`.dockerignore` исключают `.env.staging`, дампы БД, `duckdns.token`.
- `_log_config_safety` печатает только booleans/счётчики; токены/ключи/пароли не логируются.

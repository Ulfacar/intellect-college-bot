# STAGING 1 — runbook публичного supervised-пилота

Отдельный тестовый VPS. **Не production, не 8C.** Разворачивается по commit hash
deployment-коммита (создаётся после утверждения этих файлов). Application baseline —
`0d925ef` (на нём этих файлов ещё нет; он же — точка rollback приложения).

Внешний контур: `Internet → DuckDNS → Caddy(:80/:443, TLS) → app(uvicorn, 1 worker) → PostgreSQL + Redis`.
Все сервисы, кроме Caddy, живут только во внутренней docker-сети (проверено `docker compose config`).

---

## 0. Критические инварианты

- **Один worker uvicorn.** Дедуп Telegram-апдейтов, memory-locks и OpenRouter budget-reserve
  рассчитаны на один процесс. НЕ запускать `--workers>1` и не масштабировать app в несколько реплик.
- **Секреты только на сервере**, в `/etc/intellect/staging.env` (chmod 600, root). В git — никогда.
- **`ENVIRONMENT=staging`** включает fail-fast валидацию: небезопасный конфиг роняет старт.
- **Telegram — вебхуки** `/webhook/telegram/{bot_id}` с per-bot `secret_token`. Polling не используется.

## 1. Предпосылки на VPS

```bash
# Docker + Compose plugin (Compose >= 2.24.4 — для !reset/!override в overlay)
docker --version && docker compose version
sudo mkdir -p /etc/intellect /var/backups/intellect
```

## 2. Server env (секреты — на сервере, не в чат)

```bash
sudo cp .env.staging.example /etc/intellect/staging.env
sudo nano /etc/intellect/staging.env      # заполнить __...__ реальными значениями
sudo chmod 600 /etc/intellect/staging.env
# генерация значений:
openssl rand -hex 32                                   # SESSION_SECRET
docker run --rm caddy:2.11.4 caddy hash-password --plaintext 'ПАРОЛЬ'   # ADMIN_BASIC_HASH
```
Обязательные для fail-fast: `ENVIRONMENT=staging`, `PANEL_BACKEND=postgres`, `CRM_BACKEND=postgres`,
`DEMO_LOGIN=false`, сильные `ADMIN_PASSWORD` и `SESSION_SECRET(>=32)`, у каждого Telegram-бота
`token` и уникальный `webhook_secret`, `DATABASE_URL`, `REDIS_URL`.

## 3. DuckDNS + HTTPS

1. На duckdns.org создать поддомен `intellect-pilot` (имя подтверждает владелец), привязать к публичному IP VPS.
2. Token → `/etc/intellect/duckdns.token` (chmod 600). Динамический IP: `*/5 * * * * <repo>/scripts/duckdns_update.sh`
   (`DUCKDNS_SUBDOMAIN=intellect-pilot`).
3. Проверить DNS ДО TLS: `dig +short intellect-pilot.duckdns.org` == публичный IP VPS.
4. Открыть только `22, 80, 443` (напр. `ufw allow 22,80,443/tcp`). Caddy получит Let's Encrypt автоматически.
5. `caddy validate` перед первым запуском:
   ```bash
   docker run --rm -e STAGING_DOMAIN=intellect-pilot.duckdns.org -e ACME_EMAIL=you@example.com \
     -e ADMIN_BASIC_USER=support -e ADMIN_BASIC_HASH="$(docker run --rm caddy:2.11.4 caddy hash-password --plaintext x)" \
     -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2.11.4 caddy validate --config /etc/caddy/Caddyfile
   ```

## 4. Деплой (по полному commit hash)

```bash
git fetch origin
set -a; . /etc/intellect/staging.env; set +a       # для compose-level ${STAGING_DOMAIN} и т.п.
scripts/deploy.sh <ПОЛНЫЙ_40СИМВОЛЬНЫЙ_HASH_DEPLOYMENT_КОММИТА>
```
`deploy.sh`: требует чистое дерево и существующий hash → backup (если БД есть) → checkout →
`up -d --build` → ждёт `/health/ready` == 200. При ошибке — стоп + печать команды rollback.
Миграции применяются автоматически при старте (`init_db()`, идемпотентно) — отдельного шага нет.

## 5. Telegram webhook (владелец, после подтверждения домена/токенов)

```bash
scripts/set_telegram_webhooks.sh set     # setWebhook всем ботам (secret_token из env; токены не печатаются)
scripts/set_telegram_webhooks.sh info    # getWebhookInfo — проверка
scripts/set_telegram_webhooks.sh delete  # для rollback
```

## 6. Backup / Rollback

```bash
scripts/backup_db.sh                      # ручной бэкап (безопасен на первом деплое)
# ROLLBACK приложения:
git checkout <ПРЕДЫДУЩИЙ_HASH>            # напр. 0d925ef
docker compose -f docker-compose.yml -f docker-compose.staging.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.staging.yml exec -T app \
  python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready').status)"
# restore БД — ТОЛЬКО при реальной несовместимости схемы, вручную:
scripts/restore_db.sh /var/backups/intellect/college_staging_<ts>.sql.gz
```
Никакого авто-restore. Разворачиваемся по hash, не по плавающей ветке.

## 7. Логи / диагностика

```bash
docker compose -f docker-compose.yml -f docker-compose.staging.yml logs -f app     # приложение (без секретов)
docker compose -f docker-compose.yml -f docker-compose.staging.yml logs -f caddy   # reverse-proxy/TLS
```
Docker выполняет ротацию логов драйвером `json-file` (рекомендуется задать `max-size`/`max-file`
в `/etc/docker/daemon.json`). Дедуп-ключ Telegram — `bot_id:update_id` (корреляция в логах).
Приложение уже НЕ логирует токены/ключи/пароли/cookie/секреты (`_log_config_safety` печатает только booleans).

## 8. Healthchecks

- `GET /health` — liveness (app жив).
- `GET /health/ready` — readiness: `SELECT 1` + наличие критичных таблиц; 200 только когда БД готова,
  иначе 503. Не раскрывает DSN/credentials/stack. Используется Docker healthcheck, Caddy depends_on и deploy.sh.

## 9. Проверка персистентности Redis (owner §4 — до заявления «переживает restart»)

```bash
docker compose ... exec redis redis-cli set stg:probe 1
docker compose ... up -d --force-recreate redis
docker compose ... exec redis redis-cli get stg:probe      # ожидаем "1" (AOF-том redisdata сохранил)
```
Только после успеха можно утверждать, что состояние переживает recreate.

## 10. Smoke-test после развёртывания (owner §14)

1. HTTPS открывается (валидный сертификат). 2. Логин ADMIN UI v2 (нормальная авторизация).
3. DEMO_LOGIN отсутствует (`/admin/login/demo` → 404). 4. Данные Postgres переживают рестарт app.
5. Бот 1 принимает сообщение владельца. 6. Бот 2 принимает сообщение владельца.
7. Диалоги двух ботов не смешиваются (ключ `bot_id:user_id`). 8. OpenRouter отвечает.
9. Стоимость пишется (ai usage). 10. Feedback-кнопки работают (`TELEGRAM_FEEDBACK_ENABLED=true`).
11. Взять диалог. 12. Ручной ответ. 13. Вернуть боту. 14. Архивировать и вернуть из архива.
15. Global OFF останавливает автоответы. 16. Individual OFF останавливает только одного бота.
17. Ручной ответ работает при Global OFF. 18. Неизвестный TG id отклоняется до вызова LLM.
19. После рестарта данные сохраняются (Postgres + Redis §9). 20. В логах нет секретов.

## 11. Аудит публичной авторизации (по факту кода)

| Требование | Статус | Где |
|---|---|---|
| Secure cookie | ✅ | `SessionMiddleware(https_only=True)` — флаг Secure |
| HttpOnly | ✅ | Starlette SessionMiddleware всегда ставит HttpOnly |
| SameSite | ✅ | `same_site="lax"` |
| Session expiration | ✅ | `max_age=14*24*3600` (подписанная cookie с TTL) |
| Logout invalidation | ✅ | `/admin/logout`, `/admin-v2/logout` → `session.pop("manager")` |
| Login throttling | ✅ | `app/admin/ratelimit.py` — 10 провалов/60с на IP (нужен `--proxy-headers` для реального IP — есть в overlay) |
| Секреты не в HTML/логах | ✅ | пароль/секреты не рендерятся; `_log_config_safety` печатает только booleans |
| Доп. Basic Auth перед /admin* | ✅ (staging) | Caddy `basic_auth @admin` (вебхуки/health НЕ под ним) |
| **CSRF-токен на write-route** | ⚠️ ЧАСТИЧНО | явного токена нет; частичная защита `SameSite=lax` (браузер не шлёт cookie на cross-site POST) |

**План по CSRF (минимальный, до широкого доступа саппортов, отдельным согласованием):**
Cross-site POST уже смягчён `SameSite=lax` (cookie не уходит на POST с чужого origin) + доп. Basic Auth
Caddy перед `/admin*`. Полноценный фикс — синхронизатор-токен: скрытое поле CSRF в формах логина/действий
и проверка в `require_admin`/write-route. Это правка админ-слоя — **вне текущего scope STAGING 1**, выносится
отдельно. На supervised-пилоте (доступ по Basic Auth + allowlist саппортов) остаточный риск низкий.

## 12. Ограничения

- `caddy validate` и запуск стека требуют запущенного Docker-демона на VPS; локально в этой среде демон
  был недоступен — команда приведена в §3.5, выполнить на сервере.
- Alembic не сконфигурирован; схема самосоздаётся `init_db()` — корректно для ЧИСТОЙ staging-БД.
  Будущие ALTER вне `_ensure_columns` потребуют ручного `.sql`/Alembic.
- Caddy закреплён полным patch-тегом **`caddy:2.11.4`** (актуальный стабильный релиз, проверен в
  официальном registry). Директива `request_body { max_size }` требует Caddy **>= 2.10.0** — более
  старые minor-версии несовместимы. Плавающие теги (minor без полного patch, а также подвижный
  latest-тег) запрещены; инвариант закреплён тестом `tests/test_deployment_config.py`.
- `CRM_BACKEND=postgres` — локальный слой Postgres, без внешних CRM-вызовов для Telegram-пилота.

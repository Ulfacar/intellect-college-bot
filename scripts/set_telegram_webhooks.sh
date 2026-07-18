#!/usr/bin/env bash
# STAGING 1 (owner §9) — безопасная настройка Telegram webhook для ВСЕХ ботов из
# защищённого server env. НИКОГДА не печатает токены/секреты. Токены владелец НЕ вставляет
# в shell вручную — они читаются из TELEGRAM_BOTS.
#   set    — setWebhook для каждого бота (url + отдельный secret_token)
#   info   — getWebhookInfo (проверка; печатает только несекретные поля)
#   delete — deleteWebhook (для rollback)
set -euo pipefail

ACTION="${1:-set}"
ENV_FILE="${STAGING_ENV_FILE:-/etc/intellect/staging.env}"

[ -r "$ENV_FILE" ] || { echo "env-файл недоступен: $ENV_FILE" >&2; exit 1; }
# Загружаем TELEGRAM_BOTS и STAGING_DOMAIN из защищённого файла (в окружение процесса).
set -a; . "$ENV_FILE"; set +a
: "${STAGING_DOMAIN:?STAGING_DOMAIN не задан в $ENV_FILE}"
: "${TELEGRAM_BOTS:?TELEGRAM_BOTS не задан в $ENV_FILE}"

STAGING_DOMAIN="$STAGING_DOMAIN" python3 - "$ACTION" <<'PY'
import json, os, sys, urllib.request, urllib.parse

action = sys.argv[1]
domain = os.environ["STAGING_DOMAIN"]
bots = json.loads(os.environ["TELEGRAM_BOTS"])

def call(token, method, data=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = urllib.parse.urlencode(data).encode() if data else None
    with urllib.request.urlopen(url, data=body, timeout=20) as r:
        return json.load(r)

rc = 0
for b in bots:
    bid = b["id"]                      # bot_id — не секрет, печатать можно
    token = b["token"]                 # НИКОГДА не печатаем
    secret = b.get("webhook_secret", "")  # НИКОГДА не печатаем
    try:
        if action == "set":
            hook = f"https://{domain}/webhook/telegram/{bid}"
            res = call(token, "setWebhook", {"url": hook, "secret_token": secret,
                                             "drop_pending_updates": "true"})
            print(f"[{bid}] setWebhook ok={res.get('ok')} -> /webhook/telegram/{bid}")
        elif action == "delete":
            res = call(token, "deleteWebhook", {"drop_pending_updates": "false"})
            print(f"[{bid}] deleteWebhook ok={res.get('ok')}")
        elif action == "info":
            res = call(token, "getWebhookInfo")
            info = res.get("result", {})
            # только несекретные поля
            print(f"[{bid}] url={info.get('url')} pending={info.get('pending_update_count')} "
                  f"last_error={info.get('last_error_message')}")
        else:
            print(f"unknown action: {action}", file=sys.stderr); rc = 2; break
    except Exception as e:            # noqa: BLE001 — печатаем тип, но не токен/секрет
        print(f"[{bid}] ОШИБКА: {type(e).__name__}", file=sys.stderr)
        rc = 1
sys.exit(rc)
PY

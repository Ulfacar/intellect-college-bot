#!/usr/bin/env bash
# STAGING 1 (owner §4) — обновление IP поддомена DuckDNS. Token хранится ВНЕ git, в
# отдельном root-only файле. Скрипт НЕ печатает token. Для динамического IP — в cron
# (например: */5 * * * * /path/scripts/duckdns_update.sh).
set -euo pipefail

TOKEN_FILE="${DUCKDNS_TOKEN_FILE:-/etc/intellect/duckdns.token}"   # chmod 600, root
SUBDOMAIN="${DUCKDNS_SUBDOMAIN:?DUCKDNS_SUBDOMAIN не задан}"       # без .duckdns.org

[ -r "$TOKEN_FILE" ] || { echo "token-файл недоступен: $TOKEN_FILE" >&2; exit 1; }
token="$(tr -d '[:space:]' < "$TOKEN_FILE")"
[ -n "$token" ] || { echo "token пустой" >&2; exit 1; }

# ip= пусто → DuckDNS сам определит публичный IP запроса.
resp="$(curl -fsS "https://www.duckdns.org/update?domains=${SUBDOMAIN}&token=${token}&ip=")"
echo "duckdns[$SUBDOMAIN]: $resp"    # печатает OK/KO — token в вывод не попадает
[ "$resp" = "OK" ] || { echo "duckdns update не OK" >&2; exit 1; }

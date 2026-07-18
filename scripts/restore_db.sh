#!/usr/bin/env bash
# STAGING 1 (owner §7) — восстановление staging-БД из дампа. НИКОГДА не запускается
# автоматически (owner §10/§13): только вручную и только после явного подтверждения.
set -euo pipefail

FILE="${1:-}"
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.staging.yml)
DB_USER="${POSTGRES_USER:-college}"
DB_NAME="${POSTGRES_DB:-college_staging}"

[ -n "$FILE" ] || { echo "usage: restore_db.sh <backup.sql.gz>" >&2; exit 2; }
[ -r "$FILE" ] || { echo "файл недоступен: $FILE" >&2; exit 1; }

cid="$("${COMPOSE[@]}" ps -q db 2>/dev/null || true)"
[ -n "$cid" ] || { echo "контейнер db не запущен — сначала подними стек" >&2; exit 1; }

echo "ВНИМАНИЕ: это ПЕРЕЗАПИШЕТ базу '$DB_NAME' содержимым $FILE."
read -r -p "Введите 'yes' для продолжения: " answer
[ "$answer" = "yes" ] || { echo "отменено"; exit 1; }

gunzip -c "$FILE" | "${COMPOSE[@]}" exec -T db psql -U "$DB_USER" -d "$DB_NAME"
echo "[restore] завершено из $FILE"

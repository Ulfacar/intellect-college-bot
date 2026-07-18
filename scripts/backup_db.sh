#!/usr/bin/env bash
# STAGING 1 (owner §7/§10) — резервная копия staging-БД. Безопасен для первого деплоя.
# Обрабатывает: отсутствие контейнера БД (первый деплой), неготовую БД, нехватку места,
# ошибку pg_dump; retention чистит старое ТОЛЬКО после успешного нового бэкапа.
set -euo pipefail

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.staging.yml)
BACKUP_DIR="${BACKUP_DIR:-/var/backups/intellect}"
DB_USER="${POSTGRES_USER:-college}"
DB_NAME="${POSTGRES_DB:-college_staging}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
MIN_FREE_MB="${MIN_FREE_MB:-500}"

log() { echo "[backup] $*"; }

# 1. Контейнер БД существует? (первый деплой — ещё нет: не ошибка, просто нечего бэкапить)
cid="$("${COMPOSE[@]}" ps -q db 2>/dev/null || true)"
if [ -z "$cid" ]; then
  log "контейнер db не найден (первый деплой?) — бэкап пропущен"
  exit 0
fi

# 2. БД готова принимать соединения?
if ! "${COMPOSE[@]}" exec -T db pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
  log "БД не готова — прерываю, бэкап не сделан" >&2
  exit 1
fi

# 3. Достаточно места?
mkdir -p "$BACKUP_DIR"
free_mb="$(df -Pm "$BACKUP_DIR" | awk 'NR==2 {print $4}')"
if [ "${free_mb:-0}" -lt "$MIN_FREE_MB" ]; then
  log "недостаточно места: ${free_mb}MB < ${MIN_FREE_MB}MB — прерываю" >&2
  exit 1
fi

# 4. Дамп во ВРЕМЕННЫЙ файл, затем атомарный rename (чтобы битый дамп не считался бэкапом).
ts="$(date -u +%Y%m%d-%H%M%S)"
tmp="$BACKUP_DIR/.tmp-${DB_NAME}-${ts}.sql.gz"
final="$BACKUP_DIR/${DB_NAME}_${ts}.sql.gz"
if ! "${COMPOSE[@]}" exec -T db pg_dump -U "$DB_USER" -d "$DB_NAME" | gzip -c > "$tmp"; then
  rm -f "$tmp"
  log "pg_dump завершился ошибкой — бэкап не создан" >&2
  exit 1
fi
mv "$tmp" "$final"
log "готово: $final"

# 5. Retention — ТОЛЬКО после успешного нового бэкапа (owner §10).
find "$BACKUP_DIR" -maxdepth 1 -name "${DB_NAME}_*.sql.gz" -type f -mtime +"$RETENTION_DAYS" -print -delete \
  | sed 's/^/[backup] удалён старый: /' || true

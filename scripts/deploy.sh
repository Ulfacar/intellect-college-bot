#!/usr/bin/env bash
# STAGING 1 (owner §10/§13) — деплой по ПОЛНОМУ commit hash. Никакого auto-restore.
# Порядок: чистое дерево → проверка hash → backup(если БД есть) → checkout → up → readiness.
# При ошибке — стоп и показ безопасной команды rollback.
set -euo pipefail

HASH="${1:-}"
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.staging.yml)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

rollback_hint() {
  echo ""
  echo "ROLLBACK (вручную, по предыдущему hash):"
  echo "  git checkout <PREV_FULL_HASH>"
  echo "  ${COMPOSE[*]} up -d --build"
  echo "  ${COMPOSE[*]} exec -T app python -c \"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready').status)\""
  echo "  # восстановление БД (restore) — ТОЛЬКО при реальной несовместимости схемы:"
  echo "  ${SCRIPT_DIR}/restore_db.sh <backup.sql.gz>"
}
trap 'echo "[deploy] ОШИБКА — rollout остановлен."; rollback_hint' ERR

[ -n "$HASH" ] || { echo "usage: deploy.sh <full-40-char-commit-hash>" >&2; exit 2; }
[[ "$HASH" =~ ^[0-9a-f]{40}$ ]] || { echo "error: нужен ПОЛНЫЙ 40-символьный commit hash (не короткий, не ветка)" >&2; exit 2; }
[ -z "$(git status --porcelain)" ] || { echo "error: рабочее дерево не чистое — закоммить/убери изменения" >&2; exit 1; }
git cat-file -e "${HASH}^{commit}" 2>/dev/null || { echo "error: commit $HASH не найден (сделай git fetch)" >&2; exit 1; }

# Backup — только если БД уже существует (первый деплой её пропустит внутри backup_db.sh).
if [ -n "$("${COMPOSE[@]}" ps -q db 2>/dev/null || true)" ]; then
  "${SCRIPT_DIR}/backup_db.sh"
else
  echo "[deploy] БД ещё нет — бэкап не требуется (первый деплой)"
fi

echo "[deploy] checkout $HASH"
git checkout "$HASH"

echo "[deploy] сборка и запуск"
"${COMPOSE[@]}" up -d --build

echo "[deploy] ожидание readiness (/health/ready)"
ready=0
for i in $(seq 1 40); do
  if "${COMPOSE[@]}" exec -T app python -c \
      "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready').status==200 else 1)" 2>/dev/null; then
    ready=1; break
  fi
  sleep 3
done
[ "$ready" = "1" ] || { echo "[deploy] readiness не достигнут за таймаут" >&2; exit 1; }

trap - ERR
echo "[deploy] OK — задеплоен $HASH, приложение и БД готовы."
echo "[deploy] дальше (владелец): setWebhook — scripts/set_telegram_webhooks.sh set"

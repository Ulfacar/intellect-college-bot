#!/usr/bin/env bash
# Собрать прод-образ, поднять его изолированно (memory-бэкенды, без БД) и прогнать smoke.
# Запускать на любом docker-хосте (на VPS): bash scripts/smoke.sh
set -euo pipefail
cd "$(dirname "$0")/.."

IMG=frunze-smoke
PORT=18077
echo "== build =="
docker build -t "$IMG" .

echo "== run =="
CID=$(docker run -d --rm -p "${PORT}:8000" \
  -e ADMIN_PASSWORD=smoke -e SESSION_SECRET=smoke \
  -e CRM_BACKEND=stub -e PANEL_BACKEND=memory -e STATE_BACKEND=memory \
  "$IMG")
cleanup() { docker stop "$CID" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== wait for /health =="
for _ in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then break; fi
  sleep 1
done

echo "== smoke =="
BASE_URL="http://127.0.0.1:${PORT}" ADMIN_PASSWORD=smoke python3 scripts/smoke_test.py

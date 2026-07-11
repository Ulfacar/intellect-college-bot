#!/usr/bin/env python3
"""Smoke-тест на ЗАПУЩЕННОМ приложении (в идеале — на собранном прод-образе).

Ловит класс багов «работает у меня, падает в проде»: отсутствующие зависимости в
образе (напр. python-multipart), несобранные роуты, неподнятые миграции. Бьёт
ключевые эндпоинты реальными HTTP-запросами. Только стандартная библиотека.

Запуск: BASE_URL=http://127.0.0.1:18077 ADMIN_PASSWORD=smoke python scripts/smoke_test.py
"""
import http.cookiejar
import json
import os
import sys
import urllib.parse
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
ADMIN_PW = os.environ.get("ADMIN_PASSWORD", "change-me")

_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))
_fails: list[str] = []


def _req(method: str, path: str, *, data=None, json_body=None):
    headers = {}
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        resp = _opener.open(req, timeout=15)
        return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        _fails.append(msg)


def main() -> int:
    code, body = _req("GET", "/health")
    check(code == 200 and '"status":"ok"' in body, "GET /health → ok")

    # Эхо (is_me) и статус доставки не должны слать сообщений, но обязаны вернуть 200.
    echo = {"messages": [{"wh_type": "incoming_message", "is_me": True, "type": "chat",
                          "body": "smoke", "from": "1@c.us", "profile_id": "x",
                          "chat_type": "dialog", "id": "smoke-echo"}]}
    code, _ = _req("POST", "/webhook/wappi", json_body=echo)
    check(code == 200, "POST /webhook/wappi (echo) → 200")
    dlv = {"messages": [{"wh_type": "messages_status", "id": "smoke", "status": "delivered"}]}
    code, _ = _req("POST", "/webhook/wappi", json_body=dlv)
    check(code == 200, "POST /webhook/wappi (delivery) → 200")

    code, _ = _req("GET", "/admin/board/admission")
    check(code == 401, "GET /admin/board/admission без логина → 401")

    # Логин формой — заодно проверяет, что python-multipart есть в образе.
    code, _ = _req("POST", "/admin/login", data={"login": "admin", "password": ADMIN_PW})
    check(code == 200, "POST /admin/login (форма) → 200")
    code, body = _req("GET", "/admin/board/admission")
    check(code == 200, "GET /admin/board/admission после логина → 200")
    code, _ = _req("GET", "/admin/analytics")
    check(code == 200, "GET /admin/analytics → 200")

    print()
    if _fails:
        print(f"SMOKE FAILED: {len(_fails)} проблем(ы)")
        return 1
    print("SMOKE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

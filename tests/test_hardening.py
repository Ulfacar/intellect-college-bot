"""Тесты go-live hardening: проверка вебхуков, rate-limit логина, graceful-фолбэк LLM."""
import asyncio

from fastapi.testclient import TestClient

import app.main as main
from app.channels.base import Message
from app.config import BotConfig
from app.core.orchestrator import LLM_ERROR_FALLBACK, Orchestrator


class _FakeChannel:
    channel = "whatsapp"

    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return "msg-1"


# ---------------- A2: проверка секрета вебхуков ----------------
def test_webhook_rejects_wrong_secret(monkeypatch):
    monkeypatch.setattr("app.main.settings.webhook_secret", "s3cret")
    client = TestClient(main.app)
    # без секрета — 403
    assert client.post("/webhook/wappi", json={"messages": []}).status_code == 403
    # неверный секрет — 403
    assert client.post("/webhook/wappi?s=nope", json={"messages": []}).status_code == 403
    # верный секрет — пропускает (200)
    assert client.post("/webhook/wappi?s=s3cret", json={"messages": []}).status_code == 200
    # верный секрет в заголовке — тоже ок
    assert client.post("/webhook/wappi", json={"messages": []},
                       headers={"X-Webhook-Secret": "s3cret"}).status_code == 200


def test_webhook_open_when_secret_unset(monkeypatch):
    """Пустой webhook_secret → проверка выключена (обратная совместимость)."""
    monkeypatch.setattr("app.main.settings.webhook_secret", "")
    client = TestClient(main.app)
    assert client.post("/webhook/wappi", json={"messages": []}).status_code == 200


def test_telegram_webhook_secret_header(monkeypatch):
    monkeypatch.setattr("app.main.settings.webhook_secret", "tg-secret")
    client = TestClient(main.app)
    # без заголовка — 403
    assert client.post("/webhook/telegram", json={}).status_code == 403
    # с верным нативным заголовком Telegram — проходит проверку (дальше telegram_disabled, но не 403)
    r = client.post("/webhook/telegram", json={},
                    headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"})
    assert r.status_code == 200


# ---------------- A3: rate-limit логина ----------------
def test_login_rate_limited():
    from app.admin import ratelimit
    ratelimit.reset()
    client = TestClient(main.app, base_url="https://testserver")
    codes = [client.post("/admin/login", data={"login": "admin", "password": "wrong"}).status_code
             for _ in range(12)]
    assert codes[:10] == [401] * 10       # первые 10 провалов — обычный 401
    assert codes[-1] == 429               # после превышения лимита — 429
    ratelimit.reset()


# ---------------- A4: graceful-фолбэк при сбое LLM ----------------
def test_llm_failure_sends_soft_fallback(monkeypatch):
    """Если воронка падает (сбой LLM), клиент получает мягкий фолбэк, а не тишину/500."""
    monkeypatch.setattr("app.agent.llm.settings.openrouter_api_key", "x")  # llm_enabled

    async def boom(msg, state):
        raise RuntimeError("OpenRouter 503")

    from app.core import observ
    observ.reset()
    ch = _FakeChannel()
    orch = Orchestrator(channel=ch, bot=BotConfig(id="getvisa", scenario="visa"))

    import app.core.orchestrator as orch_mod

    class _F:
        async def handle(self, msg, state):
            raise RuntimeError("OpenRouter 503")

    monkeypatch.setattr(orch_mod, "get_funnel", lambda *_: _F())
    msg = Message(channel="whatsapp", user_id="996555000999", chat_id="996555000999", text="привет")
    asyncio.run(orch.handle(msg))

    assert ch.sent and ch.sent[-1][1] == LLM_ERROR_FALLBACK
    assert observ.snapshot()["llm_failures"] >= 1

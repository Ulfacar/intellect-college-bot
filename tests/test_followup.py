"""Тесты автодожима, watchdog-алертов и фонового планировщика."""
import asyncio
from datetime import datetime, timedelta, timezone

from app.core import awaiting, followup, scheduler, watchdog
from app.integrations.panel import store as ps
from app.integrations.panel.store import ConversationView


class _Cfg:
    followup_enabled = True
    followup_after_hours = 24
    noise_stale_days = 3
    followup_quiet_from = 22
    followup_quiet_to = 9
    alert_silence_minutes = 30
    alert_fail_threshold = 5
    alert_cooldown_minutes = 60
    alert_awaiting_minutes = 10

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ---------------- автодожим: отбор целей ----------------
def test_select_followup_targets():
    now = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(hours=30)
    fresh = now - timedelta(hours=2)

    def conv(uid, **kw):
        base = dict(user_id=uid, channel="whatsapp", bot_id="getvisa", chat_id=uid + "@c.us",
                    funnel="visa", stage="qualification", last_sender="bot", last_message_at=old)
        base.update(kw)
        return ConversationView(**base)

    convs = [
        conv("t1"),                                   # ← цель
        conv("c2", last_sender="client"),             # ← цель: широкое «молчит», даже client-last
        conv("s3", stage="manager"),                  # терминальная стадия
        conv("o4", outcome="won"),                    # завершён
        conv("f5", last_message_at=fresh),            # ещё не намолчался
        conv("d6", followup_sent=True),               # уже пинговали
        conv("i7", intercepted=True),                 # ведёт менеджер
        conv("ch8", channel="telegram"),              # не whatsapp
        conv("n9", stage="greeting", last_sender="client",
             last_text="https://instagram.com/ad"),   # шум не пингуем
    ]
    targets = followup.select_followup_targets(convs, now, _Cfg())
    assert [c.user_id for c in targets] == ["t1", "c2"]


def test_quiet_hours_wrap_midnight():
    cfg = _Cfg(followup_quiet_from=22, followup_quiet_to=9)
    assert followup.is_quiet_hour(23, cfg) is True
    assert followup.is_quiet_hour(3, cfg) is True
    assert followup.is_quiet_hour(12, cfg) is False


# ---------------- автодожим: отправка + идемпотентность ----------------
def test_followup_run_sends_once(monkeypatch):
    ps._memory_store._conv.clear()
    from app.core import flags
    flags.reset()
    store = ps.get_conversation_store()
    asyncio.run(store.add_message("getvisa:99", "bot", "вопрос?", channel="whatsapp",
                                  bot_id="getvisa", chat_id="99@c.us"))
    asyncio.run(store.update_meta("getvisa:99", funnel="visa", stage="qualification"))
    conv = asyncio.run(store.get("getvisa:99"))
    conv.last_message_at = datetime.now(timezone.utc) - timedelta(hours=30)  # «намолчался»

    sent = []

    async def fake_send(channel, bot_id, chat_id, text):
        sent.append((chat_id, text))
        return "pmid"

    monkeypatch.setattr(followup.outbound, "send_to_client", fake_send)
    monkeypatch.setattr(followup.settings, "followup_enabled", True)
    monkeypatch.setattr(followup.settings, "followup_after_hours", 24)
    monkeypatch.setattr(followup.settings, "followup_quiet_from", 0)  # отключить тихие часы
    monkeypatch.setattr(followup.settings, "followup_quiet_to", 0)

    asyncio.run(followup.run())
    assert sent and sent[0][0] == "99@c.us"
    conv2 = asyncio.run(store.get("getvisa:99"))
    assert conv2.followup_sent is True and conv2.stage == "follow_up"

    sent.clear()
    asyncio.run(followup.run())          # повторный прогон не шлёт второй раз
    assert sent == []


# ---------------- алерт «клиент ждёт менеджера»: отбор целей ----------------
def test_select_awaiting_targets():
    now = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(minutes=20)
    fresh = now - timedelta(minutes=3)

    def conv(uid, **kw):
        base = dict(user_id=uid, phone=uid, channel="whatsapp", bot_id="getvisa",
                    funnel="visa", stage="manager", last_sender="client",
                    last_message_at=old, outcome="manager")
        base.update(kw)
        return ConversationView(**base)

    convs = [
        conv("a1"),                                          # ← цель: хендофф, клиент ждёт 20 мин
        conv("i2", stage="qualification", intercepted=True),  # ← цель: менеджер перехватил
        conv("b3", last_sender="manager"),                   # менеджер ответил — не ждёт
        conv("b4", stage="qualification", intercepted=False),  # бот ещё ведёт сам
        conv("b5", last_message_at=fresh),                   # ждёт недолго
        conv("b6", outcome="won"),                           # завершён
    ]
    targets = awaiting.select_awaiting_targets(convs, now, _Cfg())
    assert sorted(c.user_id for c in targets) == ["a1", "i2"]


# ---------------- алерт «клиент ждёт»: отправка + cooldown ----------------
def test_awaiting_run_sends_then_cooldown(monkeypatch):
    ps._memory_store._conv.clear()
    from app.core import flags
    flags.reset()
    awaiting._alerted.clear()
    store = ps.get_conversation_store()
    asyncio.run(store.add_message("getvisa:55", "client", "алло", channel="whatsapp",
                                  bot_id="getvisa", chat_id="55@c.us", phone="996700055"))
    asyncio.run(store.update_meta("getvisa:55", funnel="visa", stage="manager"))
    conv = asyncio.run(store.get("getvisa:55"))
    conv.last_message_at = datetime.now(timezone.utc) - timedelta(minutes=20)  # ждёт давно

    sent = []

    async def fake_send(channel, bot_id, chat_id, text):
        sent.append((chat_id, text))
        return "pmid"

    monkeypatch.setattr(awaiting.outbound, "send_to_client", fake_send)
    monkeypatch.setattr(awaiting.settings, "alert_whatsapp_to", "996700@c.us")
    monkeypatch.setattr(awaiting.settings, "alert_bot_id", "getvisa")
    monkeypatch.setattr(awaiting.settings, "alert_awaiting_minutes", 10)
    monkeypatch.setattr(awaiting.settings, "alert_cooldown_minutes", 60)

    asyncio.run(awaiting.run())
    assert sent and sent[0][0] == "996700@c.us"

    sent.clear()
    asyncio.run(awaiting.run())          # cooldown по диалогу → второй раз не шлём
    assert sent == []


# ---------------- watchdog: решение об алертах ----------------
def test_watchdog_decide_silence_and_failures():
    cfg = _Cfg()
    state = {"alert_silence_ts": 0.0, "alert_fail_ts": 0.0, "fail_baseline": 0.0}
    now = 1_000_000.0

    a1 = watchdog.decide(now, 40 * 60, {"llm_failures": 0, "send_failures": 0}, state, cfg)
    assert any(k == "silence" for k, _ in a1)                 # тишина 40 мин → алерт
    a2 = watchdog.decide(now + 10, 41 * 60, {"llm_failures": 0, "send_failures": 0}, state, cfg)
    assert not any(k == "silence" for k, _ in a2)             # cooldown → повтор не шлём

    st2 = {"alert_silence_ts": 0.0, "alert_fail_ts": 0.0, "fail_baseline": 0.0}
    a3 = watchdog.decide(now, None, {"llm_failures": 4, "send_failures": 3}, st2, cfg)
    assert any(k == "failures" for k, _ in a3)                # дельта 7 ≥ порог 5


# ---------------- планировщик: прогон джоб + изоляция сбоев ----------------
def test_scheduler_runs_and_isolates_failures():
    scheduler._reset_for_tests()
    calls = []

    async def good():
        calls.append("g")

    async def bad():
        raise RuntimeError("boom")

    scheduler.register("good", good)
    scheduler.register("bad", bad)
    scheduler.register("good", good)        # дубль по имени игнорируется
    asyncio.run(scheduler.run_once())       # упавшая bad не валит прогон
    assert calls == ["g"]
    scheduler._reset_for_tests()

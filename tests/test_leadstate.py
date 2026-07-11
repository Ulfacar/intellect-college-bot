from datetime import datetime, timedelta, timezone

from app.core.leadstate import is_noise, is_silent
from app.integrations.panel.store import ConversationView, MessageView


class _Cfg:
    followup_after_hours = 24
    noise_stale_days = 3


def _conv(uid="u1", **kw):
    base = dict(
        user_id=uid,
        channel="whatsapp",
        bot_id="college_1",
        chat_id=f"{uid}@c.us",
        funnel="admission",
        stage="qualification",
        last_sender="client",
        last_text="хочу поступление",
        last_message_at=datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc),
    )
    base.update(kw)
    return ConversationView(**base)


def test_is_noise_marks_ad_link_greeting_but_not_qualified_lead():
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    spam = _conv("spam", stage="greeting", last_text="https://instagram.com/promo")
    real = _conv("real", stage="greeting", last_text="https://instagram.com/profile",
                 qualification={"name": "Алия"})

    assert is_noise(spam, now, _Cfg()) is True
    assert is_noise(real, now, _Cfg()) is False


def test_is_noise_marks_stale_empty_greeting_without_bot_or_manager_messages():
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    stale = _conv("stale", stage="greeting", last_text="добрый день",
                  last_message_at=now - timedelta(days=4),
                  messages=[MessageView("client", "добрый день", now - timedelta(days=4))])
    answered = _conv("answered", stage="greeting", last_text="Здравствуйте",
                     last_sender="bot", last_message_at=now - timedelta(days=4),
                     messages=[
                         MessageView("client", "добрый день", now - timedelta(days=4)),
                         MessageView("bot", "Здравствуйте", now - timedelta(days=4)),
                     ])

    assert is_noise(stale, now, _Cfg()) is True
    assert is_noise(answered, now, _Cfg()) is False


def test_is_silent_broad_stuck_leads_and_exclusions():
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(hours=30)
    fresh = now - timedelta(hours=2)

    assert is_silent(_conv("client-last", last_sender="client", last_message_at=old), now, _Cfg()) is True
    assert is_silent(_conv("bot-last", last_sender="bot", last_message_at=old), now, _Cfg()) is True
    assert is_silent(_conv("fresh", last_message_at=fresh), now, _Cfg()) is False
    assert is_silent(_conv("manager", stage="manager", last_message_at=old), now, _Cfg()) is False
    assert is_silent(_conv("office", stage="office", last_message_at=old), now, _Cfg()) is False
    assert is_silent(_conv("won", outcome="won", last_message_at=old), now, _Cfg()) is False
    assert is_silent(_conv("sent", followup_sent=True, last_message_at=old), now, _Cfg()) is False
    assert is_silent(_conv("spam", stage="greeting", last_text="https://instagram.com/ad",
                           last_message_at=old), now, _Cfg()) is False


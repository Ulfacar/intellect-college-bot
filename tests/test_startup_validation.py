"""STAGING 1 (owner §5) — fail-fast валидация staging-конфигурации.

validate_staging_config применяется ТОЛЬКО при ENVIRONMENT=staging; на любом другом
окружении возвращает [] (dev/тесты/прод не затронуты). Сообщения содержат имена
переменных, но НИКОГДА значения секретов.
"""
from app.config import Settings, TelegramBotConfig
from app.core.startup_validation import validate_staging_config


def _staging(**over) -> Settings:
    base = dict(
        environment="staging",
        panel_backend="postgres",
        crm_backend="postgres",
        demo_login=False,
        admin_password="a-strong-admin-password",
        session_secret="s" * 40,
        database_url="postgresql+asyncpg://u:p@db:5432/college_staging",
        state_backend="redis",
        redis_url="redis://redis:6379/0",
        telegram_bots=[
            TelegramBotConfig(id="college_test_1", token="tok1", webhook_secret="sec1"),
            TelegramBotConfig(id="college_test_2", token="tok2", webhook_secret="sec2"),
        ],
    )
    base.update(over)
    return Settings(**base)


def test_valid_staging_config_has_no_problems():
    assert validate_staging_config(_staging()) == []


def test_non_staging_env_skips_all_checks():
    # заведомо небезопасная конфигурация, но не staging → проверки не применяются.
    s = _staging(environment="dev", panel_backend="memory", crm_backend="stub", demo_login=True)
    assert validate_staging_config(s) == []


def test_memory_panel_and_stub_crm_rejected():
    p = validate_staging_config(_staging(panel_backend="memory", crm_backend="stub"))
    assert any("PANEL_BACKEND" in x for x in p)
    assert any("CRM_BACKEND" in x for x in p)


def test_demo_login_rejected():
    p = validate_staging_config(_staging(demo_login=True))
    assert any("DEMO_LOGIN" in x for x in p)


def test_placeholder_admin_password_rejected():
    for bad in ("", "change-me", "__STRONG_PASSWORD__"):
        p = validate_staging_config(_staging(admin_password=bad))
        assert any("ADMIN_PASSWORD" in x for x in p), bad


def test_weak_or_placeholder_session_secret_rejected():
    for bad in ("", "short", "change-me-college-session-secret"):
        p = validate_staging_config(_staging(session_secret=bad))
        assert any("SESSION_SECRET" in x for x in p), bad


def test_missing_bot_token_rejected():
    p = validate_staging_config(_staging(telegram_bots=[
        TelegramBotConfig(id="college_test_1", token="", webhook_secret="sec1"),
    ]))
    assert any("missing token" in x for x in p)


def test_missing_bot_webhook_secret_rejected():
    p = validate_staging_config(_staging(telegram_bots=[
        TelegramBotConfig(id="college_test_1", token="tok1", webhook_secret=""),
    ]))
    assert any("missing webhook_secret" in x for x in p)


def test_duplicate_bot_id_rejected():
    p = validate_staging_config(_staging(telegram_bots=[
        TelegramBotConfig(id="dup", token="tok1", webhook_secret="sec1"),
        TelegramBotConfig(id="dup", token="tok2", webhook_secret="sec2"),
    ]))
    assert any("duplicate bot_id" in x for x in p)


def test_duplicate_webhook_secret_rejected():
    p = validate_staging_config(_staging(telegram_bots=[
        TelegramBotConfig(id="b1", token="tok1", webhook_secret="same"),
        TelegramBotConfig(id="b2", token="tok2", webhook_secret="same"),
    ]))
    assert any("webhook_secret duplicates" in x for x in p)


def test_missing_redis_url_when_redis_backend_rejected():
    p = validate_staging_config(_staging(state_backend="redis", redis_url=""))
    assert any("REDIS_URL" in x for x in p)


def test_missing_database_url_rejected():
    p = validate_staging_config(_staging(database_url=""))
    assert any("DATABASE_URL" in x for x in p)


def test_messages_never_contain_secret_values():
    p = validate_staging_config(_staging(
        admin_password="",                       # placeholder → flagged
        session_secret="short",                  # weak → flagged
        telegram_bots=[
            TelegramBotConfig(id="b1", token="", webhook_secret="LEAKYSECRET"),
            TelegramBotConfig(id="b2", token="tokN", webhook_secret="LEAKYSECRET"),
        ],
    ))
    blob = " ".join(p)
    assert "LEAKYSECRET" not in blob       # значение webhook_secret не печатается
    assert "tokN" not in blob              # значение token не печатается
    assert p != []                         # проблемы всё же зафиксированы

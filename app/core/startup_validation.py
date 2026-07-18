"""STAGING 1 (owner §5): fail-fast конфигурация для публичного стенда.

При `ENVIRONMENT=staging` приложение обязано отказаться стартовать (до приёма трафика),
если конфигурация небезопасна для публичного supervised-пилота. `validate_staging_config`
возвращает список ПРОБЛЕМ — каждая содержит только ИМЯ переменной/причину, НИКОГДА само
значение секрета. Пустой список = конфигурация допустима.

Вызывается в `app/main.py` при старте (lifespan): непустой список → RuntimeError → uvicorn
не поднимает приложение (fail-fast), в логи не попадают значения токенов/паролей.
"""
from __future__ import annotations

# Значения-заглушки из .env.example / дефолтов кода — на staging недопустимы как реальные.
_PLACEHOLDER_SECRETS = {
    "",
    "change-me",
    "change-me-college-session-secret",
    "changeme",
    "placeholder",
    "__strong_password__",
    "__random_64_hex__",
    "__db_password__",
    "__set_in_server_env__",
}
_MIN_SESSION_SECRET_LEN = 32


def _is_placeholder(value: str) -> bool:
    return (value or "").strip().lower() in _PLACEHOLDER_SECRETS


def _is_weak_session_secret(value: str) -> bool:
    v = (value or "").strip()
    return _is_placeholder(v) or len(v) < _MIN_SESSION_SECRET_LEN


def validate_staging_config(settings) -> list[str]:
    """Список проблем конфигурации для `ENVIRONMENT=staging` (пусто = ок). Для любого
    другого окружения возвращает [] — проверки применяются ТОЛЬКО на staging и не влияют
    на dev/тесты/прод. Сообщения содержат имена переменных, но НЕ значения секретов."""
    if settings.environment != "staging":
        return []

    problems: list[str] = []

    # Персистентные бэкенды обязательны — никакой memory/stub на публичном стенде.
    if settings.panel_backend == "memory":
        problems.append("PANEL_BACKEND must not be 'memory' on staging (use 'postgres')")
    if settings.crm_backend == "stub":
        problems.append("CRM_BACKEND must not be 'stub' on staging (use 'postgres')")

    # Публичная админка: только нормальная авторизация, сильные секреты.
    if settings.demo_login:
        problems.append("DEMO_LOGIN must be false on staging")
    if _is_placeholder(settings.admin_password):
        problems.append("ADMIN_PASSWORD is empty or a placeholder value")
    if _is_weak_session_secret(settings.session_secret):
        problems.append(
            f"SESSION_SECRET is empty, shorter than {_MIN_SESSION_SECRET_LEN} chars, "
            "or a placeholder value"
        )

    # Хранилища.
    if not (settings.database_url or "").strip():
        problems.append("DATABASE_URL is required")
    if settings.state_backend == "redis" and not (settings.redis_url or "").strip():
        problems.append("REDIS_URL is required when STATE_BACKEND=redis")

    # Telegram-боты. Невалидный JSON в TELEGRAM_BOTS уже валит загрузку Settings (pydantic
    # ValidationError с именем поля, без значений) — сюда доходит уже разобранный список.
    bot_ids: set[str] = set()
    seen_secrets: set[str] = set()
    for index, bot in enumerate(settings.telegram_bots):
        label = bot.id or f"index {index}"
        if not (bot.token or "").strip():
            problems.append(f"TELEGRAM_BOTS[{label}] is missing token")
        if not (bot.webhook_secret or "").strip():
            problems.append(f"TELEGRAM_BOTS[{label}] is missing webhook_secret")
        if bot.id in bot_ids:
            problems.append(f"TELEGRAM_BOTS has a duplicate bot_id '{bot.id}'")
        bot_ids.add(bot.id)
        # Сравниваем секреты, НЕ печатая их значения.
        if bot.webhook_secret:
            if bot.webhook_secret in seen_secrets:
                problems.append(
                    f"TELEGRAM_BOTS[{label}] webhook_secret duplicates another bot's secret"
                )
            seen_secrets.add(bot.webhook_secret)

    return problems

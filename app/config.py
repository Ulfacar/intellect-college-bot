"""Конфигурация приложения (pydantic-settings, читает .env)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotConfig(BaseModel):
    """Один из продакшн-ботов: туры или визы.

    Каждый бот = свой профиль Wappi (WhatsApp-номер) + свой чат-бот Bitrix (imbot)
    в своей Открытой линии + своя CRM-воронка (category_id). Сценарий жёстко задаёт
    воронку, поэтому тур-боты не угадывают её по ключевым словам.
    Маршрутизация входящих событий Bitrix — по `bitrix_bot_id` (BOT_ID, который
    Bitrix присылает в событии imbot).
    """

    id: str                              # frunze_tours | getvisa
    scenario: Literal["tours", "visa"]
    title: str = ""                      # человекочитаемое имя профиля (FrunzeTravel…)
    manager_name: str = ""               # имя персоны в клиентских ответах
    wappi_profile_id: str = ""           # профиль Wappi Pro (WhatsApp)
    bitrix_bot_id: str = ""              # imbot BOT_ID — ключ маршрутизации входящих
    bitrix_line_id: str = ""             # CONFIG_ID Открытой линии
    category_id: str = ""                # CRM CATEGORY_ID воронки этого бота


class TelegramBotConfig(BaseModel):
    """Тестовый Telegram-бот — песочница («черновик») для обкатки правок, чтобы не
    экспериментировать на живых WhatsApp-номерах, где сидят реальные клиенты.

    Каждый бот = свой токен @BotFather + ЖЁСТКО заданный сценарий (как WhatsApp-бот):
    одна копия воронки туров, другая — виз. Поведение 1:1 с продакшн-ботами.
    """

    id: str                              # frunze_tours_tg | getvisa_tg
    scenario: Literal["tours", "visa"]
    token: str                           # токен бота от @BotFather
    title: str = ""                      # человекочитаемое имя (для логов/панели)


class ManagerConfig(BaseModel):
    """Аккаунт менеджера админ-панели. Список задаётся в env MANAGERS (JSON),
    как и BOTS. Пароль в открытом виде (как admin_password) — для простой команды
    из нескольких человек; хеширование можно добавить позже."""

    login: str
    name: str = ""
    password: str = ""


# Дефолтный реестр — 2 стартовых Wappi-бота. Реальные секреты приходят из .env
# (через JSON-переменную BOTS) либо проставляются в Фазе 0 после imbot.register.
DEFAULT_BOTS: list[BotConfig] = [
    BotConfig(id="frunze_tours", scenario="tours", title="FrunzeTravel2", manager_name="Адеми", wappi_profile_id="00000000-0000"),
    BotConfig(id="frunze_tours_sezim", scenario="tours", title="FrunzeTravel", manager_name="Сезим", wappi_profile_id="6a74fb33-16aa"),
    BotConfig(id="getvisa", scenario="visa", title="FrunzeTravel Visa", manager_name="Медина", wappi_profile_id="00000000-0000"),
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_site_url: str = ""
    openrouter_app_name: str = "Frunze Travel Bot"
    openrouter_timeout_seconds: float = 60.0
    anthropic_api_key: str = ""
    llm_model_main: str = "anthropic/claude-3.5-sonnet"
    llm_model_cheap: str = "anthropic/claude-3-haiku"

    # Каналы
    telegram_bot_token: str = ""         # дев-демо: один бот с keyword-детектом воронки (легаси)
    # Тестовые Telegram-боты (песочница «черновик»): по боту на сценарий, жёсткая воронка.
    # JSON в env: TELEGRAM_BOTS='[{"id":"frunze_tours_tg","scenario":"tours","token":"123:abc"},
    #                             {"id":"getvisa_tg","scenario":"visa","token":"456:def"}]'.
    # Вебхук каждого: POST /webhook/telegram/<id>.
    telegram_bots: list[TelegramBotConfig] = []

    # Секрет проверки входящих вебхуков (defense-in-depth поверх IP-фильтра nginx).
    # Пусто → проверка выключена (обратная совместимость). Wappi/Bitrix: ?s=<secret>
    # или заголовок X-Webhook-Secret; Telegram: заголовок X-Telegram-Bot-Api-Secret-Token.
    webhook_secret: str = ""

    # Wappi Pro (WhatsApp): token аккаунтовый, profile_id — на каждого бота (см. bots)
    wappi_base_url: str = "https://wappi.pro"
    wappi_token: str = ""
    wappi_profile_id: str = ""           # дев-демо: одиночный профиль (легаси)

    # TourVisor — кабинетные креды подходят и для XML API (проверено 19.06.2026)
    tourvisor_login: str = ""
    tourvisor_pass: str = ""

    # Хранилище
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/frunze"
    redis_url: str = "redis://localhost:6379/0"
    state_backend: Literal["memory", "redis"] = "memory"  # прод: STATE_BACKEND=redis
    state_ttl_seconds: int = 7 * 24 * 3600  # неактивный диалог живёт 7 дней

    # CRM (портал один — облако getvisakg.bitrix24.kz; вебхук портал-уровня)
    crm_backend: Literal["stub", "postgres", "bitrix24"] = "stub"
    bitrix24_webhook_url: str = ""
    # [?] Реальные значения от заказчика. Воронка → CATEGORY_ID сделки.
    bitrix_category_by_funnel: dict[str, str] = {}
    # [?] Внутренняя стадия бота → STAGE_ID канбана Bitrix (category-specific, напр. "C2:NEW").
    bitrix_stage_map: dict[str, str] = {}

    # Реестр ботов. Переопределяется JSON-строкой в env BOTS='[{...}, ...]'.
    bots: list[BotConfig] = DEFAULT_BOTS

    # Админ-панель (канбан + чат + перехват). Бэкенд лога диалогов и доступ.
    panel_backend: Literal["memory", "postgres"] = "memory"  # прод: PANEL_BACKEND=postgres
    admin_enabled: bool = True
    admin_user: str = "admin"
    admin_password: str = "frunze"  # ПРОД: переопределить ADMIN_PASSWORD!
    # Аккаунты менеджеров: JSON в env MANAGERS='[{"login":"sezim","name":"Сезим","password":"..."}]'.
    # Пусто → один менеджер из admin_user/admin_password (обратная совместимость).
    managers: list[ManagerConfig] = []
    # Watchdog-алерты: уведомлять админа в WhatsApp при тишине вебхуков / всплеске сбоев.
    # Пусто → алерты выключены. alert_bot_id — id бота (профиля), от чьего имени слать.
    alert_whatsapp_to: str = ""          # номер админа (chat_id), напр. 996700...@c.us или 996700...
    alert_bot_id: str = ""               # с какого бота слать (frunze_tours|getvisa)
    alert_silence_minutes: int = 30      # тишина дольше → алерт
    alert_fail_threshold: int = 5        # столько новых сбоев за тик → алерт
    alert_cooldown_minutes: int = 60     # не повторять один и тот же алерт чаще
    alert_awaiting_minutes: int = 10     # клиент ждёт живого менеджера дольше → алерт команде

    # Дебаунс входящих: клиенты пишут дробно (несколько коротких сообщений подряд). Бот ждёт
    # «тихое окно» этой длины, склеивает реплики и отвечает одним ходом LLM (без задвоений).
    # 0 = выключено (синхронная обработка как раньше). Прод: DEBOUNCE_SECONDS=8.
    debounce_seconds: float = 0.0

    # Автодожим: проактивный пинг клиентам, замолчавшим на этапе квалификации.
    followup_enabled: bool = False
    followup_after_hours: int = 24       # молчит дольше → один мягкий пинг
    noise_stale_days: int = 3            # пустой greeting без ответа бота старше N дней = мусор
    followup_quiet_from: int = 22        # «тихие часы» (Бишкек, UTC+6): не слать с 22:00…
    followup_quiet_to: int = 9           # …до 09:00

    # Секрет подписи cookie-сессии (Starlette SessionMiddleware). ПРОД: SESSION_SECRET!
    session_secret: str = "change-me-frunze-session-secret"
    # Быстрый вход на странице логина (кнопки «войти как …» без пароля) — ТОЛЬКО для демо.
    # Выключить (DEMO_LOGIN=false) перед боевым запуском с реальными клиентами.
    demo_login: bool = False

    def manager_list(self) -> list[ManagerConfig]:
        """Эффективный список менеджеров (с дефолтом из admin_user/admin_password)."""
        if self.managers:
            return self.managers
        return [ManagerConfig(login=self.admin_user, name="Менеджер", password=self.admin_password)]


settings = Settings()

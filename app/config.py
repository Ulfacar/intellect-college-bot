"""Application configuration."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotConfig(BaseModel):
    """One production bot profile. All college bots use the admission scenario."""

    id: str
    scenario: Literal["admission"] = "admission"
    title: str = ""
    manager_name: str = ""
    wappi_profile_id: str = ""
    bitrix_bot_id: str = ""
    bitrix_line_id: str = ""
    category_id: str = ""


class TelegramBotConfig(BaseModel):
    id: str
    scenario: Literal["admission"] = "admission"
    token: str
    title: str = ""


class ManagerConfig(BaseModel):
    login: str
    name: str = ""
    password: str = ""


DEFAULT_BOTS: list[BotConfig] = [
    BotConfig(id="college_1", scenario="admission", title="Intellect College 1", manager_name="Айдана"),
    BotConfig(id="college_2", scenario="admission", title="Intellect College 2", manager_name="Айдана"),
    BotConfig(id="college_3", scenario="admission", title="Intellect College 3", manager_name="Айдана"),
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_site_url: str = ""
    openrouter_app_name: str = "Intellect College Bot"
    openrouter_timeout_seconds: float = 60.0
    anthropic_api_key: str = ""
    llm_model_main: str = ""
    llm_model_cheap: str = ""

    telegram_bot_token: str = ""
    telegram_bots: list[TelegramBotConfig] = []
    webhook_secret: str = ""

    wappi_base_url: str = "https://wappi.pro"
    wappi_token: str = ""
    wappi_profile_id: str = ""

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/college"
    redis_url: str = "redis://localhost:6379/0"
    state_backend: Literal["memory", "redis"] = "memory"
    state_ttl_seconds: int = 7 * 24 * 3600

    crm_backend: Literal["stub", "postgres", "college_admin"] = "stub"
    college_admin_webhook_url: str = ""
    college_admin_api_key: str = ""
    college_stage_map: dict[str, str] = {}

    bots: list[BotConfig] = DEFAULT_BOTS

    panel_backend: Literal["memory", "postgres"] = "memory"
    admin_enabled: bool = True
    admin_user: str = "admin"
    admin_password: str = "change-me"
    managers: list[ManagerConfig] = []
    alert_whatsapp_to: str = ""
    alert_bot_id: str = ""
    alert_silence_minutes: int = 30
    alert_fail_threshold: int = 5
    alert_cooldown_minutes: int = 60
    alert_awaiting_minutes: int = 10

    debounce_seconds: float = 0.0
    followup_enabled: bool = False
    followup_after_hours: int = 24
    noise_stale_days: int = 3
    followup_quiet_from: int = 22
    followup_quiet_to: int = 9

    session_secret: str = "change-me-college-session-secret"
    demo_login: bool = False

    def manager_list(self) -> list[ManagerConfig]:
        if self.managers:
            return self.managers
        return [ManagerConfig(login=self.admin_user, name="Менеджер", password=self.admin_password)]


settings = Settings()

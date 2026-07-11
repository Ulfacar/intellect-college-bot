"""Реестр ботов и маршрутизация входящих событий к нужному боту.

Настроенные продакшн-боты живут на одном портале Bitrix, но в разных
Открытых линиях/каналах с разными чат-ботами (imbot). Входящее событие Bitrix несёт
BOT_ID — по нему выбираем бота, его сценарий, воронку и CRM-категорию.
"""
from __future__ import annotations

from app.config import BotConfig, settings


class BotRegistry:
    def __init__(self, bots: list[BotConfig]) -> None:
        self._bots = list(bots)
        self._by_id = {b.id: b for b in bots}
        # Только заполненные ключи — пустые id ботов (до Фазы 0) не маршрутизируем.
        self._by_bot_id = {b.bitrix_bot_id: b for b in bots if b.bitrix_bot_id}
        self._by_line = {b.bitrix_line_id: b for b in bots if b.bitrix_line_id}
        self._by_wappi = {b.wappi_profile_id: b for b in bots if b.wappi_profile_id}

    def by_id(self, bot_id: str) -> BotConfig | None:
        return self._by_id.get(bot_id)

    def by_bitrix_bot_id(self, bitrix_bot_id: str | int) -> BotConfig | None:
        """Главный ключ маршрутизации — BOT_ID из события imbot."""
        return self._by_bot_id.get(str(bitrix_bot_id))

    def by_line(self, line_id: str | int) -> BotConfig | None:
        return self._by_line.get(str(line_id))

    def by_wappi_profile_id(self, profile_id: str) -> BotConfig | None:
        """Маршрутизация прямого Wappi-канала: profile_id события → бот."""
        return self._by_wappi.get(str(profile_id))

    def all(self) -> list[BotConfig]:
        return list(self._bots)


registry = BotRegistry(settings.bots)

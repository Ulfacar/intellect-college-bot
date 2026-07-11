"""Фабрика CRM-бэкенда по конфигу (stub | bitrix24)."""
from __future__ import annotations

from functools import lru_cache

from app.config import settings
from app.integrations.crm.port import CRMPort
from app.integrations.crm.stub import CrmStub


@lru_cache
def get_crm() -> CRMPort:
    if settings.crm_backend == "bitrix24":
        from app.integrations.crm.bitrix24 import Bitrix24Crm  # фаза 2
        return Bitrix24Crm()
    if settings.crm_backend == "postgres":
        from app.integrations.crm.postgres import PostgresCrm  # персистентный локальный слой
        return PostgresCrm()
    return CrmStub()

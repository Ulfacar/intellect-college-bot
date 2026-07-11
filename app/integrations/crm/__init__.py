"""CRM backend factory."""
from __future__ import annotations

from functools import lru_cache

from app.config import settings
from app.integrations.crm.port import CRMPort
from app.integrations.crm.stub import CrmStub


@lru_cache
def get_crm() -> CRMPort:
    if settings.crm_backend == "college_admin":
        from app.integrations.crm.college_admin import CollegeAdminCrm
        return CollegeAdminCrm()
    if settings.crm_backend == "postgres":
        from app.integrations.crm.postgres import PostgresCrm  # персистентный локальный слой
        return PostgresCrm()
    return CrmStub()

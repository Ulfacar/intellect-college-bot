"""CRMPort adapter for Emir's college admin webhooks.

TODO with Emir before production:
- base URL and auth scheme;
- create lead endpoint/body and response id format;
- stage update endpoint and external stage identifiers;
- notes endpoint;
- duplicate phone behavior: upsert or new lead.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger("crm.college_admin")


class CollegeAdminCrm:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._base = settings.college_admin_webhook_url.rstrip("/")
        self._key = settings.college_admin_api_key
        self._client = client

    async def _post(self, path: str, payload: dict) -> dict:
        if not self._base:
            logger.warning("CollegeAdminCrm: webhook URL is not configured")
            return {}
        owns = self._client is None
        client = self._client or httpx.AsyncClient(timeout=20)
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        try:
            resp = await client.post(f"{self._base}{path}", json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()
        finally:
            if owns:
                await client.aclose()

    async def create_lead(self, contact: dict[str, Any], funnel: str, data: dict) -> str:
        if not self._base:
            logger.warning("CollegeAdminCrm.create_lead skipped: webhook URL is not configured")
            return ""
        payload = {
            "source": "whatsapp_bot",
            "phone": contact.get("user_id"),
            "funnel": funnel,
            "name": data.get("name"),
            "grade_base": data.get("grade_base"),
            "direction": data.get("direction"),
            "raw": data,
        }
        resp = await self._post("/leads", payload)
        lead_id = str(resp.get("id") or resp.get("result") or "")
        logger.info("CollegeAdminCrm create_lead id=%s funnel=%s", lead_id, funnel)
        return lead_id

    async def update_stage(self, deal_id: str, stage: str) -> None:
        stage_id = settings.college_stage_map.get(stage)
        if not deal_id or not stage_id:
            logger.warning("CollegeAdminCrm.update_stage skipped deal=%s stage=%s", deal_id, stage)
            return
        await self._post(f"/leads/{deal_id}/stage", {"stage": stage_id})

    async def add_note(self, deal_id: str, text: str) -> None:
        if not deal_id:
            logger.warning("CollegeAdminCrm.add_note skipped: empty deal_id")
            return
        await self._post(f"/leads/{deal_id}/notes", {"text": text})

    async def send_message(self, chat_id: str, text: str) -> None:
        logger.warning("CollegeAdminCrm.send_message is a no-op; channels send client messages")

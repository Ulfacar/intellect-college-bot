"""Funnel registry."""
from __future__ import annotations

from app.funnels.admission import AdmissionFunnel

_FUNNELS = {"admission": AdmissionFunnel()}


def get_funnel(name: str | None):
    return _FUNNELS.get(name or "admission", _FUNNELS["admission"])

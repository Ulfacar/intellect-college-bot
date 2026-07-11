"""Реестр воронок."""
from __future__ import annotations

from app.funnels.base import Funnel
from app.funnels.tickets import TicketsFunnel
from app.funnels.tours import ToursFunnel
from app.funnels.visa import VisaFunnel

_FUNNELS: dict[str, Funnel] = {
    "tours": ToursFunnel(),
    "visa": VisaFunnel(),
    "tickets": TicketsFunnel(),
}


def get_funnel(name: str) -> Funnel:
    return _FUNNELS[name]

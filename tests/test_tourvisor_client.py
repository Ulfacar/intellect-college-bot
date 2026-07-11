import asyncio
from datetime import date as real_date
from unittest.mock import AsyncMock

import app.integrations.tourvisor.client as tv
from app.integrations.tourvisor.client import (
    TourVisorClient,
    _format_over_budget,
    _format_hotels,
    _hotel_link,
    _hotel_price,
    _parse_dates,
)


class FixedDate(real_date):
    @classmethod
    def today(cls):
        return cls(2026, 6, 30)


def _hotel(name: str, price: str, currency: str = "USD") -> dict:
    return {
        "hotelname": name,
        "hotelstars": "5",
        "regionname": "Хургада",
        "tours": {
            "tour": {
                "nights": "7",
                "mealrussian": "AI",
                "price": price,
                "currency": currency,
                "operatorname": "Operator",
            }
        },
    }


def test_parse_dates_moves_past_range_to_nearest_future_year(monkeypatch):
    monkeypatch.setattr(tv, "date", FixedDate)

    assert _parse_dates("05.08.2025-12.08.2025") == ("05.08.2026", "12.08.2026")


def test_parse_dates_single_past_date_keeps_14_day_window_in_future(monkeypatch):
    monkeypatch.setattr(tv, "date", FixedDate)

    assert _parse_dates("05.08.2025") == ("05.08.2026", "19.08.2026")


def test_parse_dates_future_date_is_not_changed(monkeypatch):
    monkeypatch.setattr(tv, "date", FixedDate)

    assert _parse_dates("05.08.2026-12.08.2026") == ("05.08.2026", "12.08.2026")


def test_hotel_price_reads_best_tour_price():
    assert _hotel_price(_hotel("A", "3 153")) == 3153
    assert _hotel_price(_hotel("B", "мусор")) == 10**9


def test_format_over_budget_sorts_and_adds_notice():
    lines = _format_over_budget(
        [_hotel("Expensive", "3485"), _hotel("Cheap", "3 153"), _hotel("Middle", "3300"), _hotel("Far", "4000")],
        2000,
    )

    assert lines[0].startswith("⚠️ Под бюджет 2000 вариантов нет.")
    assert "от 3 153 USD" in lines[0]
    assert len(lines) == 4
    assert "Cheap" in lines[1]
    assert "Middle" in lines[2]
    assert "Expensive" in lines[3]


def test_format_hotels_adds_clickable_hotel_search_link():
    lines = _format_hotels([_hotel("Palmora Lara", "2612")])

    assert "ссылка: https://www.google.com/search?q=Palmora+Lara" in lines[0]
    assert "Хургада" in lines[0]


def test_hotel_link_skips_empty_default_name():
    assert _hotel_link("Отель", "Анталья") == ""


def test_search_retries_once_without_price_limit_when_budget_is_too_low(monkeypatch):
    client = TourVisorClient()
    client._login = "login"
    client._pass = "pass"
    query = {
        "departure": "80",
        "country": "1",
        "pricetype": 0,
        "pricefrom": 1000,
        "priceto": 2000,
    }
    calls: list[tuple[str, dict]] = []

    async def build_query(_http_client, _params):
        return query

    async def call(_http_client, path, params):
        calls.append((path, dict(params)))
        return {"result": {"requestid": f"request-{len(calls)}"}}

    client._build_query = build_query
    client._call = AsyncMock(side_effect=call)
    client._poll = AsyncMock(side_effect=[[], [_hotel("Cheap", "3153")]])

    result = asyncio.run(client.search({"destination": "Египет", "budget": "2000 USD"}))

    search_calls = [params for path, params in calls if path == "search.php"]
    assert len(search_calls) == 2
    assert search_calls[0]["priceto"] == 2000
    assert "priceto" not in search_calls[1]
    assert "pricefrom" not in search_calls[1]
    assert "pricetype" not in search_calls[1]
    assert result[0].startswith("⚠️ Под бюджет 2000")
    assert "Cheap" in result[1]


def test_search_returns_empty_when_retry_without_price_limit_is_empty():
    client = TourVisorClient()
    client._login = "login"
    client._pass = "pass"

    async def build_query(_http_client, _params):
        return {"departure": "80", "pricetype": 0, "priceto": 2000}

    client._build_query = build_query
    client._call = AsyncMock(side_effect=[
        {"result": {"requestid": "first"}},
        {"result": {"requestid": "second"}},
    ])
    client._poll = AsyncMock(side_effect=[[], []])

    assert asyncio.run(client.search({"budget": "2000 USD"})) == []
    assert client._call.call_count == 2
    second_params = client._call.call_args_list[1].args[2]
    assert "priceto" not in second_params

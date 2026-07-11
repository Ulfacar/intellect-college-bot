"""Deterministic FAQ layer: substring rules without LLM calls."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select

from app.config import settings
from app.core.branding import (
    FRUNZE_DESTINATIONS,
    FRUNZE_OFFICE_ADDRESS,
    FRUNZE_WORKING_HOURS,
    GETVISA_WORKING_HOURS,
    PRICE_DISCLAIMER,
    TOUR_BOOKING_TERMS,
    VISA_SERVICE_PRICES,
)

VALID_FUNNELS = {"visa", "tours", "tickets"}


@dataclass
class FaqEntryView:
    id: int = 0
    funnel: str | None = None
    enabled: bool = True
    priority: int = 0
    title: str = ""
    patterns: list[str] | None = None
    negative_terms: list[str] | None = None
    answer: str = ""
    handoff_only: bool = False
    allow_during_qualification: bool = True
    updated_by: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


def normalize_text(text: str) -> str:
    """Lowercase, normalize Russian yo, remove punctuation, collapse spaces."""
    text = (text or "").lower().replace("ё", "е")
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def match_faq(text: str, funnel: str | None, entries: list[FaqEntryView],
              *, pending_field: str | None = None) -> FaqEntryView | None:
    """Return the best matching FAQ entry, or None on no match / priority tie.

    Phase 2 may add fuzzy matching (for example rapidfuzz with a high threshold),
    but v1 stays deterministic: normalized substring only, no new dependency.
    """
    normalized = normalize_text(text)
    if not normalized:
        return None
    matches: list[FaqEntryView] = []
    for entry in entries:
        if not entry.enabled:
            continue
        if entry.funnel is not None and entry.funnel != funnel:
            continue
        if pending_field and not entry.allow_during_qualification:
            continue
        negatives = [normalize_text(x) for x in (entry.negative_terms or []) if normalize_text(x)]
        if any(term in normalized for term in negatives):
            continue
        patterns = [normalize_text(x) for x in (entry.patterns or []) if normalize_text(x)]
        if any(pattern in normalized for pattern in patterns):
            matches.append(entry)

    if not matches:
        return None
    matches.sort(key=lambda item: item.priority, reverse=True)
    if len(matches) > 1 and matches[0].priority == matches[1].priority:
        return None
    return matches[0]


def qualification_question(funnel: str | None, field: str | None) -> str | None:
    """Return the existing fallback qualification question for the current field."""
    if not funnel or not field:
        return None
    if funnel == "visa":
        from app.funnels.visa import _ask_for
    elif funnel == "tours":
        from app.funnels.tours import _ask_for
    elif funnel == "tickets":
        from app.funnels.tickets import _ask_for
    else:
        return None
    return _ask_for(field)


class MemoryFaqStore:
    def __init__(self) -> None:
        self._rows: dict[int, FaqEntryView] = {}
        self._seq = 0

    async def list(self, funnel: str | None = None, *, include_disabled: bool = True) -> list[FaqEntryView]:
        rows = list(self._rows.values())
        if funnel == "common":
            rows = [r for r in rows if r.funnel is None]
        elif funnel in VALID_FUNNELS:
            rows = [r for r in rows if r.funnel == funnel]
        if not include_disabled:
            rows = [r for r in rows if r.enabled]
        return sorted(rows, key=lambda r: (-r.priority, r.id))

    async def candidates(self, funnel: str | None) -> list[FaqEntryView]:
        rows = [r for r in self._rows.values()
                if r.enabled and (r.funnel is None or r.funnel == funnel)]
        return sorted(rows, key=lambda r: (-r.priority, r.id))

    async def get(self, entry_id: int) -> FaqEntryView | None:
        return self._rows.get(entry_id)

    async def upsert(self, data: dict[str, Any], updated_by: str = "") -> FaqEntryView:
        entry_id = int(data.get("id") or 0)
        now = datetime.now(timezone.utc)
        if entry_id and entry_id in self._rows:
            row = self._rows[entry_id]
            row.updated_at = now
        else:
            self._seq += 1
            row = FaqEntryView(id=self._seq, created_at=now, updated_at=now)
            self._rows[row.id] = row
        _apply(row, data, updated_by)
        return row

    async def set_enabled(self, entry_id: int, enabled: bool, updated_by: str = "") -> None:
        row = self._rows.get(entry_id)
        if row is None:
            return
        row.enabled = bool(enabled)
        row.updated_by = updated_by
        row.updated_at = datetime.now(timezone.utc)

    def _reset(self) -> None:
        self._rows.clear()
        self._seq = 0


class PostgresFaqStore:
    def _sm(self):
        from app.integrations.crm.db import get_sessionmaker
        return get_sessionmaker()

    async def list(self, funnel: str | None = None, *, include_disabled: bool = True) -> list[FaqEntryView]:
        from app.integrations.crm.db import FaqEntry
        async with self._sm()() as session:
            q = select(FaqEntry)
            if funnel == "common":
                q = q.where(FaqEntry.funnel.is_(None))
            elif funnel in VALID_FUNNELS:
                q = q.where(FaqEntry.funnel == funnel)
            if not include_disabled:
                q = q.where(FaqEntry.enabled.is_(True))
            rows = (await session.execute(
                q.order_by(FaqEntry.priority.desc(), FaqEntry.id.asc())
            )).scalars().all()
            return [_view(r) for r in rows]

    async def candidates(self, funnel: str | None) -> list[FaqEntryView]:
        from app.integrations.crm.db import FaqEntry
        async with self._sm()() as session:
            rows = (await session.execute(
                select(FaqEntry)
                .where(FaqEntry.enabled.is_(True))
                .where(or_(FaqEntry.funnel == funnel, FaqEntry.funnel.is_(None)))
                .order_by(FaqEntry.priority.desc(), FaqEntry.id.asc())
            )).scalars().all()
            return [_view(r) for r in rows]

    async def get(self, entry_id: int) -> FaqEntryView | None:
        from app.integrations.crm.db import FaqEntry
        async with self._sm()() as session:
            row = await session.get(FaqEntry, entry_id)
            return _view(row) if row is not None else None

    async def upsert(self, data: dict[str, Any], updated_by: str = "") -> FaqEntryView:
        from app.integrations.crm.db import FaqEntry
        async with self._sm()() as session:
            entry_id = int(data.get("id") or 0)
            row = await session.get(FaqEntry, entry_id) if entry_id else None
            if row is None:
                row = FaqEntry()
                session.add(row)
            _apply(row, data, updated_by)
            await session.commit()
            await session.refresh(row)
            return _view(row)

    async def set_enabled(self, entry_id: int, enabled: bool, updated_by: str = "") -> None:
        from app.integrations.crm.db import FaqEntry
        async with self._sm()() as session:
            row = await session.get(FaqEntry, entry_id)
            if row is None:
                return
            row.enabled = bool(enabled)
            row.updated_by = updated_by
            await session.commit()


def _apply(row: Any, data: dict[str, Any], updated_by: str) -> None:
    funnel = data.get("funnel")
    row.funnel = funnel if funnel in VALID_FUNNELS else None
    row.enabled = bool(data.get("enabled", True))
    row.priority = int(data.get("priority") or 0)
    row.title = str(data.get("title") or "").strip()
    row.patterns = [str(x).strip() for x in (data.get("patterns") or []) if str(x).strip()]
    row.negative_terms = [
        str(x).strip() for x in (data.get("negative_terms") or []) if str(x).strip()
    ]
    row.answer = str(data.get("answer") or "").strip()
    row.handoff_only = bool(data.get("handoff_only", False))
    row.allow_during_qualification = bool(data.get("allow_during_qualification", True))
    row.updated_by = updated_by


def _view(row: Any) -> FaqEntryView:
    return FaqEntryView(
        id=row.id,
        funnel=row.funnel,
        enabled=bool(row.enabled),
        priority=int(row.priority or 0),
        title=row.title or "",
        patterns=list(row.patterns or []),
        negative_terms=list(row.negative_terms or []),
        answer=row.answer or "",
        handoff_only=bool(row.handoff_only),
        allow_during_qualification=bool(row.allow_during_qualification),
        updated_by=getattr(row, "updated_by", "") or "",
        created_at=getattr(row, "created_at", None),
        updated_at=getattr(row, "updated_at", None),
    )


_memory = MemoryFaqStore()
_pg: PostgresFaqStore | None = None


def get_faq_store():
    global _pg
    if settings.panel_backend == "postgres":
        if _pg is None:
            _pg = PostgresFaqStore()
        return _pg
    return _memory


async def seed_defaults() -> None:
    """Засеять/дополнить стартовые FAQ-правила.

    Не трогаем правила, которые менеджер уже редактировал вручную. Системные правила
    (`updated_by=system:seed`) можно обновлять, чтобы прод получал новые знания без
    ручной чистки таблицы.
    """
    store = get_faq_store()
    existing = await store.list(include_disabled=True)

    defaults = [
        {
            "funnel": None,
            "enabled": True,
            "priority": 10,
            "title": "Часы работы",
            "patterns": [
                "часы работ",
                "во сколько работа",
                "во сколько вы работа",
                "режим работ",
                "график работ",
                "когда вы работа",
                "рабочее время",
                "саат канча",
                "качан иштей",
            ],
            "negative_terms": [],
            "answer": (
                f"Frunze Travel (туры/билеты): {FRUNZE_WORKING_HOURS}. "
                f"Frunze Travel (визы): {GETVISA_WORKING_HOURS}."
            ),
            "handoff_only": False,
            "allow_during_qualification": True,
        },
        {
            "funnel": None,
            "enabled": True,
            "priority": 10,
            "title": "Адрес офиса",
            "patterns": [
                "адрес",
                "где вы наход",
                "где офис",
                "как добрат",
                "где вас найти",
                "куда подъехать",
                "кайда жайгашкан",
            ],
            "negative_terms": [],
            "answer": f"Офис Frunze Travel: {FRUNZE_OFFICE_ADDRESS}.",
            "handoff_only": False,
            "allow_during_qualification": True,
        },
        {
            "funnel": "visa",
            "enabled": True,
            "priority": 30,
            "title": "Стоимость визовых услуг",
            "patterns": [
                "сколько стоит",
                "цена",
                "стоимость",
                "прайс",
                "сколько за визу",
                "канча турат",
            ],
            "negative_terms": ["тур", "билет", "авиа", "отел"],
            "answer": (
                "Подскажите, по какой стране нужна цена? Напишите страну, и я назову "
                "официальный прайс только по ней."
            ),
            "handoff_only": False,
            "allow_during_qualification": True,
        },
        {
            "funnel": "tours",
            "enabled": True,
            "priority": 20,
            "title": "Направления туров",
            "patterns": [
                "какие направлени",
                "куда можно поехать",
                "куда поехать",
                "какие страны",
                "направления",
                "кайда барса",
            ],
            "negative_terms": [],
            "answer": f"По турам работаем с такими направлениями: {FRUNZE_DESTINATIONS}",
            "handoff_only": False,
            "allow_during_qualification": True,
        },
        {
            "funnel": "visa",
            "enabled": True,
            "priority": 35,
            "title": "Self-visa удержание",
            "patterns": [
                "сам оформлю",
                "сама оформлю",
                "сам подам",
                "без вас",
                "селф виза",
                "self visa",
                "самостоятельно оформлю",
            ],
            "negative_terms": [],
            "answer": (
                "Понимаю, можно попробовать самостоятельно. Мы полезны тем, что проверяем анкету, "
                "снижаем риск ошибок и готовим к интервью. Хотите, я передам менеджеру на короткую консультацию?"
            ),
            "handoff_only": False,
            "allow_during_qualification": True,
        },
        {
            "funnel": "visa",
            "enabled": True,
            "priority": 25,
            "title": "Гарантии по визе",
            "patterns": [
                "гарантия визы",
                "гарантируете",
                "точно дадут",
                "100 процентов",
                "шансы",
            ],
            "negative_terms": [],
            "answer": (
                "Визу мы не гарантируем, решение принимает консульство. Мы помогаем грамотно заполнить анкету, "
                "подготовиться к интервью и снизить риск ошибок."
            ),
            "handoff_only": False,
            "allow_during_qualification": True,
        },
        {
            "funnel": "visa",
            "enabled": True,
            "priority": 25,
            "title": "Документы для визы США",
            "patterns": [
                "документы сша",
                "что нужно для сша",
                "какие документы для визы сша",
                "документы на американскую визу",
            ],
            "negative_terms": [],
            "answer": (
                "Для туристической визы США обычно нужны загранпаспорт и справка с работы. "
                "Полный набор зависит от вашей ситуации, его уточнит эксперт на консультации."
            ),
            "handoff_only": False,
            "allow_during_qualification": True,
        },
        {
            "funnel": "visa",
            "enabled": True,
            "priority": 25,
            "title": "Отказ в визе",
            "patterns": [
                "был отказ",
                "отказали в визе",
                "после отказа",
                "отказ сша",
            ],
            "negative_terms": [],
            "answer": (
                "После отказа подаваться повторно можно, обычно важно показать изменения в ситуации. "
                "Скажите, пожалуйста, в какой стране и в каком году был отказ?"
            ),
            "handoff_only": False,
            "allow_during_qualification": True,
        },
        {
            "funnel": "tours",
            "enabled": True,
            "priority": 25,
            "title": "Бронь и оплата тура",
            "patterns": [
                "как забронировать",
                "что нужно для брони",
                "предоплата",
                "оплата тура",
                "паспорт для тура",
            ],
            "negative_terms": [],
            "answer": f"Для брони нужен загранпаспорт. {TOUR_BOOKING_TERMS}",
            "handoff_only": False,
            "allow_during_qualification": True,
        },
        {
            "funnel": "tours",
            "enabled": True,
            "priority": 25,
            "title": "Почему цена тура меняется",
            "patterns": [
                "почему цена меняется",
                "цена изменилась",
                "актуальная цена",
                "почему дороже",
            ],
            "negative_terms": [],
            "answer": PRICE_DISCLAIMER,
            "handoff_only": False,
            "allow_during_qualification": True,
        },
    ]
    existing_by_key = {(row.funnel, row.title): row for row in existing}
    for row in defaults:
        current = existing_by_key.get((row["funnel"], row["title"]))
        if current is None:
            await store.upsert(row, updated_by="system:seed")
            continue
        if current.updated_by in {"", "system:seed"}:
            await store.upsert({**row, "id": current.id}, updated_by="system:seed")


def reset() -> None:
    _memory._reset()

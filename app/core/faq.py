"""Deterministic FAQ layer: substring rules without LLM calls."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select

from app.config import settings
from app.core.branding import COLLEGE_ADDRESS

VALID_FUNNELS = {"admission"}


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
    if funnel == "admission":
        from app.funnels.admission import _ask_for
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
                "Точные часы работы приёмной комиссии сейчас уточняются. "
                "Передала вопрос менеджеру — он ответит здесь в рабочее время."
            ),
            "handoff_only": True,
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
            "answer": (
                f"Мы находимся по адресу: {COLLEGE_ADDRESS}. Будем рады видеть вас! "
                "Подсказать что-то по поступлению?"
            ),
            "handoff_only": False,
            "allow_during_qualification": True,
        },
        {
            "funnel": "admission",
            "enabled": True,
            "priority": 40,
            "title": "Стоимость обучения",
            "patterns": [
                "сколько стоит",
                "цена",
                "стоимость",
                "прайс",
                "оплата",
                "канча турат",
                "окуу канча",
            ],
            "negative_terms": ["за год", "за курс", "за все", "за всё"],
            "answer": (
                "Стоимость обучения по контракту — 6500 долларов. После вступительного теста "
                "предусмотрена персональная скидка, её размер озвучит менеджер. Хотите, запишу вас на тест?"
            ),
            "handoff_only": False,
            "allow_during_qualification": True,
        },
        {
            "funnel": "admission",
            "enabled": True,
            "priority": 35,
            "title": "Направления",
            "patterns": [
                "какие направлени",
                "какие специальности",
                "какие профессии",
                "направления",
                "багыт",
                "кандай багыт",
            ],
            "negative_terms": [],
            "answer": (
                "У нас 8 направлений. IT: кибербезопасность и этичный хакинг, программная инженерия и ИИ, "
                "DevOps, Front-end/Back-end веб-разработка, создание цифровых продуктов. Бизнес: "
                "бизнес-аналитика и финансы, маркетинг, менеджмент, графический дизайн и UX/UI. "
                "Какое из них вам ближе?"
            ),
            "handoff_only": False,
            "allow_during_qualification": True,
        },
        {
            "funnel": "admission",
            "enabled": True,
            "priority": 35,
            "title": "Вступительный тест",
            "patterns": [
                "что за тест",
                "какой тест",
                "предметы тест",
                "вступительный тест",
                "тест кандай",
                "кирүү тест",
            ],
            "negative_terms": [],
            "answer": (
                "Вступительный тест — по математике и английскому языку, длится 1,5 часа. "
                "Дату, время и формат подтвердит менеджер при записи. Записать вас?"
            ),
            "handoff_only": False,
            "allow_during_qualification": True,
        },
        {
            "funnel": "admission",
            "enabled": True,
            "priority": 25,
            "title": "Гарантии поступления",
            "patterns": [
                "гарантия поступления",
                "гарантируете",
                "точно поступлю",
                "точно поступит",
                "точно пройду",
                "100 процентов",
                "грант",
                "кепилдик",
            ],
            "negative_terms": [],
            "answer": (
                "Поступление, грант и прохождение теста мы не гарантируем. Менеджер честно подскажет "
                "условия и следующий шаг по вашей ситуации."
            ),
            "handoff_only": False,
            "allow_during_qualification": True,
        },
        {
            "funnel": "admission",
            "enabled": True,
            "priority": 25,
            "title": "Документы",
            "patterns": [
                "документы",
                "какие документы",
                "что нужно из документов",
                "документ",
                "кандай документ",
            ],
            "negative_terms": [],
            "answer": (
                "Нужно свидетельство о рождении. Если уже есть паспорт — достаточно только паспорта. "
                "Подсказать что-то ещё по поступлению?"
            ),
            "handoff_only": False,
            "allow_during_qualification": True,
        },
        {
            "funnel": "admission",
            "enabled": True,
            "priority": 25,
            "title": "Дедлайн приёма",
            "patterns": [
                "до какого числа",
                "дедлайн",
                "до когда прием",
                "до когда приём",
                "качанга чейин",
                "качан чейин",
            ],
            "negative_terms": [],
            "answer": (
                "Приём идёт до 12 августа. Лучше не откладывать: успеем и тест пройти, и всё оформить. "
                "Вы после 9 или после 11 класса?"
            ),
            "handoff_only": False,
            "allow_during_qualification": True,
        },
        {
            "funnel": "admission",
            "enabled": True,
            "priority": 25,
            "title": "После 9 или 11 класса",
            "patterns": [
                "после 9",
                "после девятого",
                "после 11",
                "после одиннадцатого",
                "9 класса",
                "11 класса",
                "9-класстан",
                "11-класстан",
            ],
            "negative_terms": [],
            "answer": (
                "Да, мы принимаем и после 9, и после 11 класса. После 9 класса обучение длится "
                "2 года 10 месяцев. Вам какой вариант актуален?"
            ),
            "handoff_only": False,
            "allow_during_qualification": True,
        },
        {
            "funnel": "admission",
            "enabled": True,
            "priority": 25,
            "title": "Скидка",
            "patterns": [
                "скидка",
                "скидки",
                "арзандатуу",
                "арзандат",
            ],
            "negative_terms": [],
            "answer": (
                "Да, скидки есть — персональная скидка определяется после вступительного теста, "
                "размер озвучит менеджер. Давайте запишу вас на тест?"
            ),
            "handoff_only": False,
            "allow_during_qualification": True,
        },
        {
            "funnel": "admission",
            "enabled": True,
            "priority": 30,
            "title": "Проходной балл",
            "patterns": ["проходной балл", "порог", "сколько баллов", "өтүү балл"],
            "negative_terms": [],
            "answer": (
                "Точный проходной балл подскажет менеджер, я передала ему ваш вопрос — ответит здесь. "
                "Могу пока рассказать, из чего состоит тест, хотите?"
            ),
            "handoff_only": True,
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

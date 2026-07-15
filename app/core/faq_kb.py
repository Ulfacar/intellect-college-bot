"""Increment 5: managed multilingual FAQ / knowledge base — storage layer.

NEW, additive tables (`faq_kb_entries`/`faq_kb_variants`/`faq_kb_versions`/
`faq_kb_answer_log`, see `app/integrations/crm/db.py`) — does NOT touch or replace the
legacy single-language `faq_entries` table / `app/core/faq.py` (LEGACY, untouched;
`backfill_legacy` below only READS it).

Two backends behind one contract (same pattern as `app/integrations/panel/leadstore.py`):
`MemoryFaqKbStore` (default: tests/offline) and `PostgresFaqKbStore` (prod). Selected via
`settings.panel_backend`, same switch as the rest of the telegram-pilot canonical stores.

Publication lifecycle (see `docs/faq-knowledge-base-spec.md` + Increment 5 design):
- `publication_status` (draft/published/archived) and `enabled`/`archived_at` are LIVE
  governance fields on the entry row — Disable/Enable/Archive act on them immediately,
  no publish cycle required.
- `canonical_question`/`answer_ru`/`answer_ky`/`category`/`priority`/`handoff_only`/
  `valid_from`/`valid_until` + variants are the entry's CURRENT content — editable at
  any time via `update_draft`, but this does NOT change what the bot serves.
- The bot ONLY ever serves the snapshot of the entry's LATEST `faq_kb_versions` row
  with `action IN ('published', 'restored')` — see `list_published_candidates`. Publish
  snapshots current content into a new version row (action='published') and stamps
  `published_by`/`published_at` on the entry. Rollback snapshots an OLD version's
  content into a NEW version row (action='restored') AND copies it back onto the live
  entry fields (so the editor shows what's now being served) — history is never
  mutated or deleted, only appended to.
- Publish and Rollback are each ONE transaction (entry update + version insert).
  Answer-log writes (`log_answer`) are separate/best-effort by design (see
  `app/core/telegram_commands.py` — never allowed to break the reply on failure).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.core.faq_matcher import MatchCandidate, VariantText, normalize_text

# --------------------------------------------------------------------------------------
# Categories (human RU labels live in the admin UI, see app/admin/router.py).
# --------------------------------------------------------------------------------------

CATEGORIES: list[str] = [
    "general", "contacts", "schedule", "admission", "documents", "directions",
    "tuition", "discounts", "payment", "entrance_test", "passing_score", "deadlines",
    "contract", "infrastructure", "employment", "international", "other",
]
SENSITIVE_CATEGORIES: frozenset[str] = frozenset({
    "tuition", "discounts", "payment", "entrance_test", "passing_score", "deadlines", "contract",
})
PUBLICATION_STATUSES = {"draft", "published", "archived"}
VERSION_ACTIONS = {"created", "edited", "published", "disabled", "enabled", "archived", "restored"}
_SERVING_ACTIONS = {"published", "restored"}   # which version rows the bot may serve


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_category(value: Any) -> str:
    return value if value in CATEGORIES else "other"


# --------------------------------------------------------------------------------------
# View dataclasses (UI/tests do not depend on the ORM).
# --------------------------------------------------------------------------------------

@dataclass
class FaqKbEntryView:
    id: int = 0
    canonical_question: str = ""
    answer_ru: str = ""
    answer_ky: str | None = None
    category: str = "general"
    priority: int = 0
    publication_status: str = "draft"
    enabled: bool = True
    handoff_only: bool = False
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    created_by: str | None = None
    updated_by: str | None = None
    published_by: str | None = None
    published_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    archived_at: datetime | None = None

    @property
    def is_sensitive(self) -> bool:
        return self.category in SENSITIVE_CATEGORIES

    @property
    def missing_ky(self) -> bool:
        return not bool(self.answer_ky)

    def is_expired(self, *, now: datetime | None = None) -> bool:
        now = now or _now()
        return bool(self.valid_until and self.valid_until < now)


@dataclass
class FaqKbVariantView:
    id: int = 0
    faq_entry_id: int = 0
    text: str = ""
    language: str | None = None
    normalized_text: str = ""
    created_at: datetime | None = None


@dataclass
class FaqKbVersionView:
    id: int = 0
    faq_entry_id: int = 0
    version_number: int = 1
    snapshot: dict[str, Any] = field(default_factory=dict)
    action: str = "created"
    actor: str | None = None
    created_at: datetime | None = None


@dataclass
class ActionResult:
    """Outcome of a lifecycle action (publish/rollback/disable/enable/archive)."""

    ok: bool
    entry: FaqKbEntryView | None = None
    error: str | None = None   # not_found | archived | confirmation_required | invalid_version


# --------------------------------------------------------------------------------------
# Snapshot helpers (shared by both backends).
# --------------------------------------------------------------------------------------

def _dt_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _dt_parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def build_snapshot(entry: FaqKbEntryView, variants: list[FaqKbVariantView]) -> dict[str, Any]:
    return {
        "canonical_question": entry.canonical_question,
        "answer_ru": entry.answer_ru,
        "answer_ky": entry.answer_ky,
        "category": entry.category,
        "priority": entry.priority,
        "handoff_only": entry.handoff_only,
        "valid_from": _dt_iso(entry.valid_from),
        "valid_until": _dt_iso(entry.valid_until),
        "variants": [{"id": v.id, "text": v.text, "language": v.language} for v in variants],
    }


def snapshot_to_candidate(
    faq_entry_id: int, faq_version_id: int | None, snapshot: dict[str, Any],
) -> MatchCandidate:
    variants = [
        VariantText(v.get("id"), v.get("text", ""), v.get("language"))
        for v in (snapshot.get("variants") or [])
        if v.get("text")
    ]
    return MatchCandidate(
        faq_entry_id=faq_entry_id,
        canonical_question=snapshot.get("canonical_question", ""),
        variants=variants,
        answer_ru=snapshot.get("answer_ru", ""),
        answer_ky=snapshot.get("answer_ky"),
        category=snapshot.get("category", "other"),
        priority=int(snapshot.get("priority") or 0),
        handoff_only=bool(snapshot.get("handoff_only")),
        faq_version_id=faq_version_id,
    )


def _snapshot_in_window(snapshot: dict[str, Any], *, now: datetime) -> bool:
    valid_from = _dt_parse(snapshot.get("valid_from"))
    valid_until = _dt_parse(snapshot.get("valid_until"))
    if valid_from and now < valid_from:
        return False
    if valid_until and now > valid_until:
        return False
    return True


def _entry_candidate(entry: FaqKbEntryView, variants: list[FaqKbVariantView]) -> MatchCandidate:
    """Live-content candidate for admin "preview draft" — bypasses publish/version
    entirely (never touches the real pipeline, see `app/admin/router.py` playground)."""
    return MatchCandidate(
        faq_entry_id=entry.id,
        canonical_question=entry.canonical_question,
        variants=[VariantText(v.id, v.text, v.language) for v in variants if v.text],
        answer_ru=entry.answer_ru,
        answer_ky=entry.answer_ky,
        category=entry.category,
        priority=entry.priority,
        handoff_only=entry.handoff_only,
        faq_version_id=None,
    )


def _clean_entry_data(data: dict[str, Any]) -> dict[str, Any]:
    valid_from = data.get("valid_from")
    valid_until = data.get("valid_until")
    return {
        "canonical_question": str(data.get("canonical_question") or "").strip(),
        "answer_ru": str(data.get("answer_ru") or "").strip(),
        "answer_ky": (str(data["answer_ky"]).strip() or None) if data.get("answer_ky") else None,
        "category": _clean_category(data.get("category")),
        "priority": int(data.get("priority") or 0),
        "handoff_only": bool(data.get("handoff_only", False)),
        "valid_from": valid_from,
        "valid_until": valid_until,
    }


def _clean_variants(raw: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for item in raw or []:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        language = item.get("language") or None
        if language not in ("ru", "ky", None):
            language = None
        out.append({"text": text, "language": language})
    return out


# --------------------------------------------------------------------------------------
# Memory backend
# --------------------------------------------------------------------------------------

class MemoryFaqKbStore:
    def __init__(self) -> None:
        self._entries: dict[int, FaqKbEntryView] = {}
        self._entry_seq = 0
        self._variants: dict[int, list[FaqKbVariantView]] = {}
        self._variant_seq = 0
        self._versions: dict[int, list[FaqKbVersionView]] = {}
        self._version_seq = 0
        self._answer_log: list[dict[str, Any]] = []
        self._answer_log_seq = 0

    def _reset(self) -> None:
        self.__init__()  # type: ignore[misc]

    # ---- entries / variants ----

    async def get_entry(self, entry_id: int) -> FaqKbEntryView | None:
        return self._entries.get(entry_id)

    async def list_variants(self, entry_id: int) -> list[FaqKbVariantView]:
        return list(self._variants.get(entry_id, []))

    async def list_versions(self, entry_id: int) -> list[FaqKbVersionView]:
        return sorted(self._versions.get(entry_id, []), key=lambda v: v.version_number)

    async def list_entries(
        self, *, status: str | None = None, category: str | None = None,
        enabled: bool | None = None, missing_ky: bool | None = None,
        handoff_only: bool | None = None, expired: bool | None = None, search: str | None = None,
    ) -> list[FaqKbEntryView]:
        rows = list(self._entries.values())
        rows = _apply_filters(
            rows, status=status, category=category, enabled=enabled, missing_ky=missing_ky,
            handoff_only=handoff_only, expired=expired, search=search,
        )
        return sorted(rows, key=lambda r: (-r.priority, r.id))

    async def create_draft(self, data: dict[str, Any], variants: list[dict[str, Any]], actor: str) -> FaqKbEntryView:
        self._entry_seq += 1
        now = _now()
        cleaned = _clean_entry_data(data)
        entry = FaqKbEntryView(
            id=self._entry_seq, **cleaned, publication_status="draft", enabled=True,
            created_by=actor, updated_by=actor, created_at=now, updated_at=now,
        )
        self._entries[entry.id] = entry
        self._save_variants(entry.id, variants)
        self._write_version(entry, "created", actor)
        return entry

    async def update_draft(self, entry_id: int, data: dict[str, Any], variants: list[dict[str, Any]], actor: str) -> FaqKbEntryView | None:
        entry = self._entries.get(entry_id)
        if entry is None:
            return None
        cleaned = _clean_entry_data(data)
        for key, value in cleaned.items():
            setattr(entry, key, value)
        entry.updated_by = actor
        entry.updated_at = _now()
        self._save_variants(entry_id, variants)
        self._write_version(entry, "edited", actor)
        return entry

    def _save_variants(self, entry_id: int, variants: list[dict[str, Any]]) -> None:
        now = _now()
        rows = []
        for v in _clean_variants(variants):
            self._variant_seq += 1
            rows.append(FaqKbVariantView(
                id=self._variant_seq, faq_entry_id=entry_id, text=v["text"], language=v["language"],
                normalized_text=normalize_text(v["text"]), created_at=now,
            ))
        self._variants[entry_id] = rows

    def _write_version(self, entry: FaqKbEntryView, action: str, actor: str | None) -> FaqKbVersionView:
        self._version_seq += 1
        existing = self._versions.setdefault(entry.id, [])
        version_number = (max((v.version_number for v in existing), default=0)) + 1
        row = FaqKbVersionView(
            id=self._version_seq, faq_entry_id=entry.id, version_number=version_number,
            snapshot=build_snapshot(entry, self._variants.get(entry.id, [])),
            action=action, actor=actor, created_at=_now(),
        )
        existing.append(row)
        return row

    # ---- lifecycle actions ----

    async def publish(self, entry_id: int, actor: str, *, confirm: bool = False) -> ActionResult:
        entry = self._entries.get(entry_id)
        if entry is None:
            return ActionResult(False, error="not_found")
        if entry.publication_status == "archived":
            return ActionResult(False, entry=entry, error="archived")
        if entry.is_sensitive and not confirm:
            return ActionResult(False, entry=entry, error="confirmation_required")
        self._write_version(entry, "published", actor)
        now = _now()
        entry.publication_status = "published"
        entry.published_by = actor
        entry.published_at = now
        entry.updated_by = actor
        entry.updated_at = now
        return ActionResult(True, entry=entry)

    async def rollback(self, entry_id: int, version_number: int, actor: str, *, confirm: bool = False) -> ActionResult:
        entry = self._entries.get(entry_id)
        if entry is None:
            return ActionResult(False, error="not_found")
        if not confirm:
            return ActionResult(False, entry=entry, error="confirmation_required")
        target = next((v for v in self._versions.get(entry_id, []) if v.version_number == version_number), None)
        if target is None:
            return ActionResult(False, entry=entry, error="invalid_version")
        snap = target.snapshot
        entry.canonical_question = snap.get("canonical_question", "")
        entry.answer_ru = snap.get("answer_ru", "")
        entry.answer_ky = snap.get("answer_ky")
        entry.category = _clean_category(snap.get("category"))
        entry.priority = int(snap.get("priority") or 0)
        entry.handoff_only = bool(snap.get("handoff_only"))
        entry.valid_from = _dt_parse(snap.get("valid_from"))
        entry.valid_until = _dt_parse(snap.get("valid_until"))
        self._save_variants(entry_id, snap.get("variants") or [])
        now = _now()
        self._write_version(entry, "restored", actor)
        entry.publication_status = "published"
        entry.published_by = actor
        entry.published_at = now
        entry.updated_by = actor
        entry.updated_at = now
        return ActionResult(True, entry=entry)

    async def disable(self, entry_id: int, actor: str, *, confirm: bool = False) -> ActionResult:
        entry = self._entries.get(entry_id)
        if entry is None:
            return ActionResult(False, error="not_found")
        if entry.publication_status == "published" and not confirm:
            return ActionResult(False, entry=entry, error="confirmation_required")
        entry.enabled = False
        entry.updated_by = actor
        entry.updated_at = _now()
        self._write_version(entry, "disabled", actor)
        return ActionResult(True, entry=entry)

    async def enable(self, entry_id: int, actor: str) -> ActionResult:
        entry = self._entries.get(entry_id)
        if entry is None:
            return ActionResult(False, error="not_found")
        entry.enabled = True
        entry.updated_by = actor
        entry.updated_at = _now()
        self._write_version(entry, "enabled", actor)
        return ActionResult(True, entry=entry)

    async def archive(self, entry_id: int, actor: str, *, confirm: bool = False) -> ActionResult:
        entry = self._entries.get(entry_id)
        if entry is None:
            return ActionResult(False, error="not_found")
        if not confirm:
            return ActionResult(False, entry=entry, error="confirmation_required")
        now = _now()
        entry.publication_status = "archived"
        entry.archived_at = now
        entry.updated_by = actor
        entry.updated_at = now
        self._write_version(entry, "archived", actor)
        return ActionResult(True, entry=entry)

    # ---- matching / playground ----

    async def list_published_candidates(self, *, now: datetime | None = None) -> list[MatchCandidate]:
        now = now or _now()
        out: list[MatchCandidate] = []
        for entry in self._entries.values():
            if not entry.enabled or entry.publication_status != "published" or entry.archived_at is not None:
                continue
            versions = [v for v in self._versions.get(entry.id, []) if v.action in _SERVING_ACTIONS]
            if not versions:
                continue
            latest = max(versions, key=lambda v: v.version_number)
            if not _snapshot_in_window(latest.snapshot, now=now):
                continue
            out.append(snapshot_to_candidate(entry.id, latest.id, latest.snapshot))
        return out

    async def get_entry_candidate(self, entry_id: int) -> MatchCandidate | None:
        entry = self._entries.get(entry_id)
        if entry is None:
            return None
        return _entry_candidate(entry, self._variants.get(entry_id, []))

    # ---- answer log (best-effort) ----

    async def log_answer(self, **fields: Any) -> None:
        self._answer_log_seq += 1
        fields["id"] = self._answer_log_seq
        fields.setdefault("created_at", _now())
        self._answer_log.append(fields)

    async def list_answer_log(self) -> list[dict[str, Any]]:
        return list(self._answer_log)

    # ---- legacy backfill ----

    async def backfill_legacy(self, actor: str = "system:backfill") -> int:
        return await _run_backfill(self, actor)


def _apply_filters(
    rows: list[FaqKbEntryView], *, status, category, enabled, missing_ky, handoff_only, expired, search,
) -> list[FaqKbEntryView]:
    now = _now()
    out = rows
    if status:
        out = [r for r in out if r.publication_status == status]
    if category:
        out = [r for r in out if r.category == category]
    if enabled is not None:
        out = [r for r in out if r.enabled == enabled]
    if missing_ky:
        out = [r for r in out if r.missing_ky]
    if handoff_only is not None:
        out = [r for r in out if r.handoff_only == handoff_only]
    if expired is not None:
        out = [r for r in out if r.is_expired(now=now) == expired]
    if search:
        needle = search.strip().lower()
        if needle:
            out = [r for r in out if needle in (r.canonical_question or "").lower()]
    return out


# --------------------------------------------------------------------------------------
# Postgres backend
# --------------------------------------------------------------------------------------

class PostgresFaqKbStore:
    def __init__(self, sessionmaker: async_sessionmaker | None = None) -> None:
        self._sessionmaker = sessionmaker

    def _sm(self) -> async_sessionmaker:
        if self._sessionmaker is None:
            from app.integrations.crm.db import get_sessionmaker
            self._sessionmaker = get_sessionmaker()
        return self._sessionmaker

    async def get_entry(self, entry_id: int) -> FaqKbEntryView | None:
        from app.integrations.crm.db import FaqKbEntry
        async with self._sm()() as session:
            row = await session.get(FaqKbEntry, entry_id)
            return _entry_view(row) if row is not None else None

    async def list_variants(self, entry_id: int) -> list[FaqKbVariantView]:
        from app.integrations.crm.db import FaqKbVariant
        async with self._sm()() as session:
            rows = (await session.execute(
                select(FaqKbVariant).where(FaqKbVariant.faq_entry_id == entry_id).order_by(FaqKbVariant.id)
            )).scalars().all()
            return [_variant_view(r) for r in rows]

    async def list_versions(self, entry_id: int) -> list[FaqKbVersionView]:
        from app.integrations.crm.db import FaqKbVersion
        async with self._sm()() as session:
            rows = (await session.execute(
                select(FaqKbVersion).where(FaqKbVersion.faq_entry_id == entry_id)
                .order_by(FaqKbVersion.version_number)
            )).scalars().all()
            return [_version_view(r) for r in rows]

    async def list_entries(
        self, *, status: str | None = None, category: str | None = None,
        enabled: bool | None = None, missing_ky: bool | None = None,
        handoff_only: bool | None = None, expired: bool | None = None, search: str | None = None,
    ) -> list[FaqKbEntryView]:
        from app.integrations.crm.db import FaqKbEntry
        async with self._sm()() as session:
            q = select(FaqKbEntry)
            if status:
                q = q.where(FaqKbEntry.publication_status == status)
            if category:
                q = q.where(FaqKbEntry.category == category)
            if enabled is not None:
                q = q.where(FaqKbEntry.enabled.is_(enabled))
            if handoff_only is not None:
                q = q.where(FaqKbEntry.handoff_only.is_(handoff_only))
            if search:
                q = q.where(FaqKbEntry.canonical_question.ilike(f"%{search.strip()}%"))
            rows = (await session.execute(q)).scalars().all()
            views = [_entry_view(r) for r in rows]
            if missing_ky:
                views = [v for v in views if v.missing_ky]
            if expired is not None:
                views = [v for v in views if v.is_expired() == expired]
            return sorted(views, key=lambda r: (-r.priority, r.id))

    async def create_draft(self, data: dict[str, Any], variants: list[dict[str, Any]], actor: str) -> FaqKbEntryView:
        from app.integrations.crm.db import FaqKbEntry, FaqKbVariant, FaqKbVersion
        cleaned = _clean_entry_data(data)
        async with self._sm()() as session:
            async with session.begin():
                row = FaqKbEntry(**cleaned, publication_status="draft", enabled=True,
                                  created_by=actor, updated_by=actor)
                session.add(row)
                await session.flush()
                await session.refresh(row)  # populate server-computed created_at/updated_at before reading them
                variant_rows = _insert_variants(session, row.id, variants)
                snapshot = build_snapshot(_entry_view(row), [_variant_view(v) for v in variant_rows])
                session.add(FaqKbVersion(faq_entry_id=row.id, version_number=1, snapshot=snapshot,
                                          action="created", actor=actor))
            await session.refresh(row)
            return _entry_view(row)

    async def update_draft(self, entry_id: int, data: dict[str, Any], variants: list[dict[str, Any]], actor: str) -> FaqKbEntryView | None:
        from app.integrations.crm.db import FaqKbEntry
        cleaned = _clean_entry_data(data)
        async with self._sm()() as session:
            async with session.begin():
                row = await session.get(FaqKbEntry, entry_id)
                if row is None:
                    return None
                for key, value in cleaned.items():
                    setattr(row, key, value)
                row.updated_by = actor
                await session.flush()
                await session.refresh(row)  # populate server-computed updated_at before reading it
                variant_rows = await _replace_variants(session, entry_id, variants)
                snapshot = build_snapshot(_entry_view(row), [_variant_view(v) for v in variant_rows])
                await _insert_version(session, entry_id, snapshot, "edited", actor)
            await session.refresh(row)
            return _entry_view(row)

    async def publish(self, entry_id: int, actor: str, *, confirm: bool = False) -> ActionResult:
        from app.integrations.crm.db import FaqKbEntry, FaqKbVariant
        async with self._sm()() as session:
            async with session.begin():
                row = await session.get(FaqKbEntry, entry_id)
                if row is None:
                    return ActionResult(False, error="not_found")
                if row.publication_status == "archived":
                    return ActionResult(False, entry=_entry_view(row), error="archived")
                if row.category in SENSITIVE_CATEGORIES and not confirm:
                    return ActionResult(False, entry=_entry_view(row), error="confirmation_required")
                variant_rows = (await session.execute(
                    select(FaqKbVariant).where(FaqKbVariant.faq_entry_id == entry_id)
                )).scalars().all()
                snapshot = build_snapshot(_entry_view(row), [_variant_view(v) for v in variant_rows])
                await _insert_version(session, entry_id, snapshot, "published", actor)
                now = _now()
                row.publication_status = "published"
                row.published_by = actor
                row.published_at = now
                row.updated_by = actor
            await session.refresh(row)
            return ActionResult(True, entry=_entry_view(row))

    async def rollback(self, entry_id: int, version_number: int, actor: str, *, confirm: bool = False) -> ActionResult:
        from app.integrations.crm.db import FaqKbEntry, FaqKbVersion
        async with self._sm()() as session:
            async with session.begin():
                row = await session.get(FaqKbEntry, entry_id)
                if row is None:
                    return ActionResult(False, error="not_found")
                if not confirm:
                    return ActionResult(False, entry=_entry_view(row), error="confirmation_required")
                target = (await session.execute(
                    select(FaqKbVersion).where(FaqKbVersion.faq_entry_id == entry_id)
                    .where(FaqKbVersion.version_number == version_number)
                )).scalar_one_or_none()
                if target is None:
                    return ActionResult(False, entry=_entry_view(row), error="invalid_version")
                snap = target.snapshot
                row.canonical_question = snap.get("canonical_question", "")
                row.answer_ru = snap.get("answer_ru", "")
                row.answer_ky = snap.get("answer_ky")
                row.category = _clean_category(snap.get("category"))
                row.priority = int(snap.get("priority") or 0)
                row.handoff_only = bool(snap.get("handoff_only"))
                row.valid_from = _dt_parse(snap.get("valid_from"))
                row.valid_until = _dt_parse(snap.get("valid_until"))
                await session.flush()
                await _replace_variants(session, entry_id, snap.get("variants") or [])
                await _insert_version(session, entry_id, snap, "restored", actor)
                now = _now()
                row.publication_status = "published"
                row.published_by = actor
                row.published_at = now
                row.updated_by = actor
            await session.refresh(row)
            return ActionResult(True, entry=_entry_view(row))

    async def disable(self, entry_id: int, actor: str, *, confirm: bool = False) -> ActionResult:
        from app.integrations.crm.db import FaqKbEntry, FaqKbVariant
        async with self._sm()() as session:
            async with session.begin():
                row = await session.get(FaqKbEntry, entry_id)
                if row is None:
                    return ActionResult(False, error="not_found")
                if row.publication_status == "published" and not confirm:
                    return ActionResult(False, entry=_entry_view(row), error="confirmation_required")
                row.enabled = False
                row.updated_by = actor
                await session.flush()
                await session.refresh(row)  # populate server-computed updated_at before reading it
                variant_rows = (await session.execute(
                    select(FaqKbVariant).where(FaqKbVariant.faq_entry_id == entry_id)
                )).scalars().all()
                snapshot = build_snapshot(_entry_view(row), [_variant_view(v) for v in variant_rows])
                await _insert_version(session, entry_id, snapshot, "disabled", actor)
            await session.refresh(row)
            return ActionResult(True, entry=_entry_view(row))

    async def enable(self, entry_id: int, actor: str) -> ActionResult:
        from app.integrations.crm.db import FaqKbEntry, FaqKbVariant
        async with self._sm()() as session:
            async with session.begin():
                row = await session.get(FaqKbEntry, entry_id)
                if row is None:
                    return ActionResult(False, error="not_found")
                row.enabled = True
                row.updated_by = actor
                await session.flush()
                await session.refresh(row)  # populate server-computed updated_at before reading it
                variant_rows = (await session.execute(
                    select(FaqKbVariant).where(FaqKbVariant.faq_entry_id == entry_id)
                )).scalars().all()
                snapshot = build_snapshot(_entry_view(row), [_variant_view(v) for v in variant_rows])
                await _insert_version(session, entry_id, snapshot, "enabled", actor)
            await session.refresh(row)
            return ActionResult(True, entry=_entry_view(row))

    async def archive(self, entry_id: int, actor: str, *, confirm: bool = False) -> ActionResult:
        from app.integrations.crm.db import FaqKbEntry, FaqKbVariant
        async with self._sm()() as session:
            async with session.begin():
                row = await session.get(FaqKbEntry, entry_id)
                if row is None:
                    return ActionResult(False, error="not_found")
                if not confirm:
                    return ActionResult(False, entry=_entry_view(row), error="confirmation_required")
                now = _now()
                row.publication_status = "archived"
                row.archived_at = now
                row.updated_by = actor
                await session.flush()
                await session.refresh(row)  # populate server-computed updated_at before reading it
                variant_rows = (await session.execute(
                    select(FaqKbVariant).where(FaqKbVariant.faq_entry_id == entry_id)
                )).scalars().all()
                snapshot = build_snapshot(_entry_view(row), [_variant_view(v) for v in variant_rows])
                await _insert_version(session, entry_id, snapshot, "archived", actor)
            await session.refresh(row)
            return ActionResult(True, entry=_entry_view(row))

    async def list_published_candidates(self, *, now: datetime | None = None) -> list[MatchCandidate]:
        from app.integrations.crm.db import FaqKbEntry, FaqKbVersion
        now = now or _now()
        async with self._sm()() as session:
            rows = (await session.execute(
                select(FaqKbEntry)
                .where(FaqKbEntry.enabled.is_(True))
                .where(FaqKbEntry.publication_status == "published")
                .where(FaqKbEntry.archived_at.is_(None))
            )).scalars().all()
            out: list[MatchCandidate] = []
            for row in rows:
                versions = (await session.execute(
                    select(FaqKbVersion).where(FaqKbVersion.faq_entry_id == row.id)
                    .where(FaqKbVersion.action.in_(_SERVING_ACTIONS))
                    .order_by(FaqKbVersion.version_number.desc())
                    .limit(1)
                )).scalars().all()
                if not versions:
                    continue
                latest = versions[0]
                if not _snapshot_in_window(latest.snapshot, now=now):
                    continue
                out.append(snapshot_to_candidate(row.id, latest.id, latest.snapshot))
            return out

    async def get_entry_candidate(self, entry_id: int) -> MatchCandidate | None:
        from app.integrations.crm.db import FaqKbVariant
        entry = await self.get_entry(entry_id)
        if entry is None:
            return None
        async with self._sm()() as session:
            rows = (await session.execute(
                select(FaqKbVariant).where(FaqKbVariant.faq_entry_id == entry_id)
            )).scalars().all()
            return _entry_candidate(entry, [_variant_view(r) for r in rows])

    async def log_answer(self, **fields: Any) -> None:
        from app.integrations.crm.db import FaqKbAnswerLog
        async with self._sm()() as session:
            row = FaqKbAnswerLog(**fields)
            session.add(row)
            await session.commit()

    async def list_answer_log(self) -> list[dict[str, Any]]:
        from app.integrations.crm.db import FaqKbAnswerLog
        async with self._sm()() as session:
            rows = (await session.execute(select(FaqKbAnswerLog))).scalars().all()
            return [
                {c.name: getattr(r, c.name) for c in r.__table__.columns}
                for r in rows
            ]

    async def backfill_legacy(self, actor: str = "system:backfill") -> int:
        return await _run_backfill(self, actor)


def _insert_variants(session, entry_id: int, variants: list[dict[str, Any]]):
    from app.integrations.crm.db import FaqKbVariant
    rows = []
    for v in _clean_variants(variants):
        row = FaqKbVariant(
            faq_entry_id=entry_id, text=v["text"], language=v["language"],
            normalized_text=normalize_text(v["text"]),
        )
        session.add(row)
        rows.append(row)
    return rows


async def _replace_variants(session, entry_id: int, variants: list[dict[str, Any]]):
    from app.integrations.crm.db import FaqKbVariant
    existing = (await session.execute(
        select(FaqKbVariant).where(FaqKbVariant.faq_entry_id == entry_id)
    )).scalars().all()
    for row in existing:
        await session.delete(row)
    await session.flush()
    rows = _insert_variants(session, entry_id, variants)
    await session.flush()
    return rows


async def _insert_version(session, entry_id: int, snapshot: dict[str, Any], action: str, actor: str) -> None:
    from sqlalchemy import func as sa_func

    from app.integrations.crm.db import FaqKbVersion
    current_max = (await session.execute(
        select(sa_func.max(FaqKbVersion.version_number)).where(FaqKbVersion.faq_entry_id == entry_id)
    )).scalar()
    version_number = (current_max or 0) + 1
    session.add(FaqKbVersion(
        faq_entry_id=entry_id, version_number=version_number, snapshot=snapshot,
        action=action, actor=actor,
    ))


def _entry_view(row: Any) -> FaqKbEntryView:
    return FaqKbEntryView(
        id=row.id, canonical_question=row.canonical_question or "", answer_ru=row.answer_ru or "",
        answer_ky=row.answer_ky, category=row.category or "other", priority=int(row.priority or 0),
        publication_status=row.publication_status or "draft", enabled=bool(row.enabled),
        handoff_only=bool(row.handoff_only), valid_from=row.valid_from, valid_until=row.valid_until,
        created_by=row.created_by, updated_by=row.updated_by, published_by=row.published_by,
        published_at=row.published_at, created_at=row.created_at, updated_at=row.updated_at,
        archived_at=row.archived_at,
    )


def _variant_view(row: Any) -> FaqKbVariantView:
    return FaqKbVariantView(
        id=row.id, faq_entry_id=row.faq_entry_id, text=row.text or "", language=row.language,
        normalized_text=row.normalized_text or "", created_at=row.created_at,
    )


def _version_view(row: Any) -> FaqKbVersionView:
    return FaqKbVersionView(
        id=row.id, faq_entry_id=row.faq_entry_id, version_number=row.version_number,
        snapshot=dict(row.snapshot or {}), action=row.action, actor=row.actor, created_at=row.created_at,
    )


# --------------------------------------------------------------------------------------
# Legacy backfill (idempotent — guarded by a `created_by="legacy_backfill:<legacy_id>"`
# marker so re-running never double-imports). Never auto-runs at startup; exposed as a
# callable (admin action / one-off script) — see app/admin/router.py.
# --------------------------------------------------------------------------------------

# Best-effort NON-sensitive category keywords only — a sensitive-sounding legacy rule
# (price/discount/passing score/...) intentionally falls through to "other" rather than
# being auto-tagged into a sensitive category (see docstring + task spec).
_BACKFILL_CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("contacts", ["адрес", "где вы наход", "как добрат", "где офис"]),
    ("schedule", ["часы работ", "график работ", "режим работ", "рабочее время"]),
    ("documents", ["документ"]),
    ("directions", ["направлени", "специальност", "профессии"]),
    ("admission", ["класс", "поступлен", "прием", "приём", "дедлайн", "до какого числа"]),
]


def _guess_legacy_category(title: str, patterns: list[str]) -> str:
    haystack = " ".join([title or "", *(patterns or [])]).lower()
    for category, keywords in _BACKFILL_CATEGORY_KEYWORDS:
        if any(kw in haystack for kw in keywords):
            return category
    return "other"


async def _run_backfill(store: "MemoryFaqKbStore | PostgresFaqKbStore", actor: str) -> int:
    from app.core.faq import get_faq_store as get_legacy_store

    legacy_store = get_legacy_store()
    legacy_rows = await legacy_store.list(funnel=None, include_disabled=True)

    already_imported = {
        marker.split(":", 1)[1]
        for marker in await _existing_backfill_markers(store)
    }

    imported = 0
    for legacy in legacy_rows:
        legacy_key = str(legacy.id)
        if legacy_key in already_imported:
            continue
        category = _guess_legacy_category(legacy.title, legacy.patterns or [])
        data = {
            "canonical_question": legacy.title,
            "answer_ru": legacy.answer,
            "answer_ky": None,
            "category": category,
            "priority": legacy.priority,
            "handoff_only": legacy.handoff_only,
        }
        variants = [{"text": p, "language": None} for p in (legacy.patterns or [])]
        marker = f"legacy_backfill:{legacy_key}"
        entry = await store.create_draft(data, variants, marker)
        if not legacy.enabled:
            await store.disable(entry.id, marker, confirm=True)
        imported += 1
    return imported


async def _existing_backfill_markers(store: "MemoryFaqKbStore | PostgresFaqKbStore") -> list[str]:
    entries = await store.list_entries()
    return [e.created_by for e in entries if e.created_by and e.created_by.startswith("legacy_backfill:")]


# --------------------------------------------------------------------------------------
# Singletons
# --------------------------------------------------------------------------------------

_memory = MemoryFaqKbStore()
_pg: PostgresFaqKbStore | None = None


def get_faq_kb_store():
    global _pg
    if settings.panel_backend == "postgres":
        if _pg is None:
            _pg = PostgresFaqKbStore()
        return _pg
    return _memory


def reset() -> None:
    """Сброс memory-стора (для тестов)."""
    _memory._reset()

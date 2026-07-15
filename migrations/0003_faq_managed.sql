-- Migration 0003: managed multilingual FAQ / knowledge base (Telegram pilot, Increment 5).
--
-- There is NO Alembic in this project — SQLAlchemy `Base.metadata.create_all()`
-- (app/integrations/crm/db.py::init_db/init_models) creates these same four tables on
-- app startup (additive, never drops), together with everything else on `Base`. This
-- file is a standalone, idempotent, manually-runnable equivalent for operators who
-- prefer plain SQL / CI migration steps.
--
-- Additive only:
--   - Creates FOUR NEW tables: `faq_kb_entries`, `faq_kb_variants`, `faq_kb_versions`,
--     `faq_kb_answer_log`.
--   - Does NOT alter, rename or drop anything from migrations 0001/0002 or the legacy
--     `conversations`/`messages`/`deals`/`faq_entries`/`audit_log` tables. In
--     particular, `faq_entries` (the LEGACY single-language deterministic FAQ used by
--     `app/core/faq.py`) is UNTOUCHED — `faq_kb_entries` below is a completely
--     separate table; the two never collide.
--   - Safe to run multiple times (CREATE TABLE/INDEX IF NOT EXISTS).
--   - No secrets, no real college answers here — this file ships no seed data.
--
-- Canonical rules (see docs/faq-knowledge-base-spec.md, app/core/faq_kb.py,
-- app/core/faq_matcher.py for the full contract):
--   - `faq_kb_entries.canonical_question`/`answer_ru`/`answer_ky`/`category`/
--     `priority`/`handoff_only`/`valid_from`/`valid_until` are the entry's CURRENT,
--     possibly-unpublished, editable content. The bot NEVER reads these columns
--     directly — it only ever serves the snapshot of the entry's LATEST
--     `faq_kb_versions` row with `action IN ('published','restored')`. Editing a
--     published entry therefore does NOT change production until "Publish" is
--     pressed again.
--   - `enabled`/`publication_status`/`archived_at` are LIVE governance flags checked
--     directly on the entry row (Disable/Enable/Archive act immediately, no publish
--     cycle). `publication_status` is one of draft|published|archived. A `draft` entry
--     NEVER answers; neither does a `disabled` (enabled=false) or `archived` one, nor
--     one outside its published snapshot's [valid_from, valid_until] window.
--   - Hard delete of a published entry is forbidden — "Delete" = archive (soft:
--     `archived_at` set + `publication_status='archived'`), history is kept forever.
--   - `faq_kb_versions` never has rows deleted or mutated — Publish and Rollback each
--     APPEND one new row (`action='published'`/`'restored'`) inside a single
--     transaction together with the entry-row update.
--   - `faq_kb_answer_log` is a best-effort write (see `app/core/telegram_commands.py`
--     `_try_faq_reply`) — minimal prep for Increment 7 only (which FAQ answer was
--     sent); it deliberately carries NO LLM tokens/cost/model columns.

CREATE TABLE IF NOT EXISTS faq_kb_entries (
    id                   BIGSERIAL PRIMARY KEY,
    canonical_question   TEXT         NOT NULL DEFAULT '',
    answer_ru            TEXT         NOT NULL DEFAULT '',
    answer_ky            TEXT,
    category             VARCHAR(32)  NOT NULL DEFAULT 'general',
    priority             INTEGER      NOT NULL DEFAULT 0,
    -- draft | published | archived
    publication_status   VARCHAR(16)  NOT NULL DEFAULT 'draft',
    enabled              BOOLEAN      NOT NULL DEFAULT TRUE,
    handoff_only         BOOLEAN      NOT NULL DEFAULT FALSE,
    valid_from           TIMESTAMPTZ,
    valid_until          TIMESTAMPTZ,
    created_by           VARCHAR(64),
    updated_by           VARCHAR(64),
    published_by         VARCHAR(64),
    published_at         TIMESTAMPTZ,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    archived_at          TIMESTAMPTZ
);

-- Sensitive categories (tuition, discounts, payment, entrance_test, passing_score,
-- deadlines, contract) require an explicit confirm flag before Publish/Disable —
-- enforced in application code (app/core/faq_kb.py), not as a DB CHECK constraint.

CREATE INDEX IF NOT EXISTS ix_faq_kb_entries_publication_status ON faq_kb_entries (publication_status);
CREATE INDEX IF NOT EXISTS ix_faq_kb_entries_category ON faq_kb_entries (category);
CREATE INDEX IF NOT EXISTS ix_faq_kb_entries_enabled ON faq_kb_entries (enabled);
CREATE INDEX IF NOT EXISTS ix_faq_kb_entries_valid_from ON faq_kb_entries (valid_from);
CREATE INDEX IF NOT EXISTS ix_faq_kb_entries_valid_until ON faq_kb_entries (valid_until);


CREATE TABLE IF NOT EXISTS faq_kb_variants (
    id                BIGSERIAL PRIMARY KEY,
    faq_entry_id      BIGINT       NOT NULL REFERENCES faq_kb_entries (id),
    text              TEXT         NOT NULL DEFAULT '',
    language          VARCHAR(8),                          -- ru | ky | NULL (both)
    normalized_text   TEXT         NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Variants are STRUCTURED rows (one per question-variant) — never a comma-joined
-- string column. Replaced wholesale on every "Save draft" (see app/core/faq_kb.py).

CREATE INDEX IF NOT EXISTS ix_faq_kb_variants_entry_id ON faq_kb_variants (faq_entry_id);


CREATE TABLE IF NOT EXISTS faq_kb_versions (
    id                BIGSERIAL PRIMARY KEY,
    faq_entry_id      BIGINT       NOT NULL REFERENCES faq_kb_entries (id),
    version_number    INTEGER      NOT NULL DEFAULT 1,
    snapshot          JSON         NOT NULL DEFAULT '{}',
    -- created | edited | published | disabled | enabled | archived | restored
    action            VARCHAR(16)  NOT NULL DEFAULT 'created',
    actor             VARCHAR(64),
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Only rows with action IN ('published','restored') are ever served to the bot (the
-- latest by version_number, per entry) — the rest exist purely for audit/history.
-- `snapshot` contains ONLY entry content (question/answers/category/priority/
-- handoff_only/valid window/variants) — NO secrets, tokens, prompts, or conversations.

CREATE INDEX IF NOT EXISTS ix_faq_kb_versions_entry_version ON faq_kb_versions (faq_entry_id, version_number);


CREATE TABLE IF NOT EXISTS faq_kb_answer_log (
    id                    BIGSERIAL PRIMARY KEY,
    conversation_id       BIGINT       NOT NULL,
    client_message_id     VARCHAR(64),
    bot_message_id        VARCHAR(64),
    source                VARCHAR(16)  NOT NULL DEFAULT 'faq',
    faq_entry_id          BIGINT       NOT NULL,
    faq_version_id        BIGINT,
    matched_variant_id    BIGINT,
    match_type            VARCHAR(16)  NOT NULL DEFAULT '',   -- canonical|variant|normalized|fuzzy
    match_score           DOUBLE PRECISION,
    language              VARCHAR(8)   NOT NULL DEFAULT 'ru',
    missing_answer_ky     BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_faq_kb_answer_log_conversation_id ON faq_kb_answer_log (conversation_id);
CREATE INDEX IF NOT EXISTS ix_faq_kb_answer_log_faq_entry_id ON faq_kb_answer_log (faq_entry_id);


-- ---------------------------------------------------------------------------------------
-- Legacy backfill (NOT run automatically by this file or by anything at startup).
--
-- `app/core/faq_kb.py::backfill_legacy()` (async Python, idempotent — guarded by a
-- `created_by = 'legacy_backfill:<legacy faq_entries.id>'` marker so re-running never
-- double-imports) reads the LEGACY `faq_entries` table and inserts one DRAFT
-- `faq_kb_entries` row per legacy rule: `answer_ru = faq_entries.answer`,
-- `answer_ky = NULL`, `patterns[] -> faq_kb_variants` (structured), `category` is a
-- best-effort NON-sensitive guess (falls back to 'other' — a legacy rule that reads as
-- a sensitive fact, e.g. price/discount/passing-score, is intentionally NEVER
-- auto-tagged into a sensitive category), `enabled`/`handoff_only`/`priority` carried
-- over as-is. Every imported row starts and stays `publication_status='draft'` — it is
-- NEVER auto-published; a manager must review and explicitly Publish each one from the
-- admin UI. The legacy `faq_entries` table is only READ, never modified.
--
-- Trigger it from a Python shell / one-off admin action, e.g.:
--   python -c "import asyncio; from app.core.faq_kb import get_faq_kb_store; \
--              asyncio.run(get_faq_kb_store().backfill_legacy())"
-- ---------------------------------------------------------------------------------------


-- ---------------------------------------------------------------------------------------
-- Rollback (manual only — NOT executed automatically by anything). Run these
-- statements by hand if this increment must be reverted. They drop ONLY the four new
-- tables created above; migrations 0001/0002 and everything legacy (including
-- `faq_entries`) are untouched.
-- ---------------------------------------------------------------------------------------
-- DROP TABLE IF EXISTS faq_kb_answer_log;
-- DROP TABLE IF EXISTS faq_kb_versions;
-- DROP TABLE IF EXISTS faq_kb_variants;
-- DROP TABLE IF EXISTS faq_kb_entries;

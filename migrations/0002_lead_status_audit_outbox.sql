-- Migration 0002: lead_status audit trail + outbox stub (Telegram pilot, Increment 3).
--
-- There is NO Alembic in this project — SQLAlchemy `Base.metadata.create_all()`
-- (app/integrations/crm/db.py::init_db/init_models) creates these same two tables on
-- app startup (additive, never drops), together with everything else on `Base`
-- (including 0001's `leads`/`pilot_conversations`). This file is a standalone,
-- idempotent, manually-runnable equivalent for operators who prefer plain SQL / CI
-- migration steps.
--
-- Additive only:
--   - Creates two NEW tables: `lead_audit`, `outbox`.
--   - Does NOT alter, rename or drop anything from migration 0001 or the legacy
--     `conversations`/`messages`/`deals`/`faq_entries`/`audit_log` tables.
--   - Safe to run multiple times (CREATE TABLE/INDEX IF NOT EXISTS).
--   - No real personal data / secrets in this file.
--
-- Canonical rules (see docs/phase1-implementation-plan.md,
-- docs/admin-bot-control-and-ai-classification-spec.md):
--   - `lead_audit` is written ONLY by app/core/lead_status_service.py::LeadStatusService
--     (event_type = lead_status_changed | status_change_blocked) and
--     app/core/conversation_service.py::ConversationService (event_type =
--     dialog_owner_changed). `bot_phase_changed` is reserved for future use.
--   - `outbox` gets a row ONLY on a REAL `lead_status` change (never for no-op, a
--     manual-lock block, a dialog_owner/bot_phase change, legacy mirroring, or
--     reads/backfill). There is no real Trello worker in Phase 1 — events stay
--     `pending` until a Phase 2 consumer is wired up by a separate command.

CREATE TABLE IF NOT EXISTS lead_audit (
    id                  BIGSERIAL PRIMARY KEY,
    lead_id             BIGINT       REFERENCES leads (id),
    conversation_id     BIGINT       REFERENCES pilot_conversations (id),
    -- lead_status_changed | dialog_owner_changed | bot_phase_changed | status_change_blocked
    event_type          VARCHAR(32)  NOT NULL,
    previous_status     VARCHAR(32),
    new_status          VARCHAR(32),
    previous_owner      VARCHAR(16),
    new_owner           VARCHAR(16),
    source              VARCHAR(16)  NOT NULL DEFAULT '',   -- bot|admin|trello|system
    actor               VARCHAR(64),
    reason              TEXT,
    confidence          DOUBLE PRECISION,
    metadata            JSON,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- event_type is exactly one of: lead_status_changed, dialog_owner_changed,
-- bot_phase_changed, status_change_blocked (enforced in application code, not as a DB
-- CHECK constraint, to keep this migration additive/low-risk).
-- No secrets, no full prompts, no extra PII — only status/owner codes and short
-- reason/actor strings.

CREATE INDEX IF NOT EXISTS ix_lead_audit_lead_id ON lead_audit (lead_id);
CREATE INDEX IF NOT EXISTS ix_lead_audit_event_type ON lead_audit (event_type);


CREATE TABLE IF NOT EXISTS outbox (
    id                  BIGSERIAL PRIMARY KEY,
    aggregate_type      VARCHAR(32)  NOT NULL DEFAULT 'lead',
    aggregate_id        BIGINT       NOT NULL,
    event_type          VARCHAR(32)  NOT NULL DEFAULT 'lead_status_changed',
    payload             JSON         NOT NULL DEFAULT '{}',
    idempotency_key     VARCHAR(160) NOT NULL,
    status              VARCHAR(16)  NOT NULL DEFAULT 'pending',   -- pending|processed|error
    attempts            INTEGER      NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    processed_at        TIMESTAMPTZ,
    last_error          TEXT
);

-- idempotency_key format: lead:<lead_id>:status:<target_status>:change:<audit_id>
-- (unique per real lead_status change — prevents accidental duplicate outbox events).

CREATE UNIQUE INDEX IF NOT EXISTS ux_outbox_idempotency_key ON outbox (idempotency_key);
CREATE INDEX IF NOT EXISTS ix_outbox_status ON outbox (status);
CREATE INDEX IF NOT EXISTS ix_outbox_aggregate_id ON outbox (aggregate_id);


-- ---------------------------------------------------------------------------------------
-- Increment 4 suggestion (NOT applied now — commented only): a Postgres partial unique
-- index to enforce the "at most one active session per (bot_id, external_user_id)"
-- invariant at the database level, on top of the application-level ConflictError guard
-- added in app/integrations/panel/leadstore.py::create_conversation (Increment 3). Left
-- commented because concurrent-write races are out of scope for this increment (single
-- writer / sequential tests); revisit together with a real concurrency story.
-- ---------------------------------------------------------------------------------------
-- CREATE UNIQUE INDEX IF NOT EXISTS ux_pilot_conversations_active_session
--     ON pilot_conversations (bot_id, external_user_id)
--     WHERE archived_at IS NULL;


-- ---------------------------------------------------------------------------------------
-- Rollback (manual only — NOT executed automatically by anything). Run these two
-- statements by hand if this increment must be reverted. They drop ONLY the two new
-- tables created above; migration 0001's tables and everything legacy are untouched.
-- ---------------------------------------------------------------------------------------
-- DROP TABLE IF EXISTS outbox;
-- DROP TABLE IF EXISTS lead_audit;

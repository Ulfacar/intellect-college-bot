-- Migration 0001: canonical Conversation and Lead models (Telegram pilot, Increment 2).
--
-- There is NO Alembic in this project — SQLAlchemy `Base.metadata.create_all()`
-- (app/integrations/crm/db.py::init_db) creates these same two tables on app startup
-- (additive, never drops). This file is a standalone, idempotent, manually-runnable
-- equivalent for operators who prefer plain SQL / CI migration steps.
--
-- Additive only:
--   - Creates two NEW tables: `leads`, `pilot_conversations`.
--   - Does NOT alter, rename or drop the legacy `conversations`/`messages` tables
--     (app/integrations/crm/db.py::Conversation/ConvMessage), which remain owned by the
--     existing admin panel (app/integrations/panel/store.py::ConversationView). The name
--     `conversations` is already taken by that legacy table, hence the new canonical
--     conversation table is named `pilot_conversations`.
--   - Safe to run multiple times (CREATE TABLE/INDEX IF NOT EXISTS).
--   - No real personal data / secrets in this file.
--
-- Canonical rule (see docs/phase1-implementation-plan.md, docs/admin-bot-control-and-
-- ai-classification-spec.md): `lead_status` lives ONLY in `leads` (PostgreSQL) — it is
-- never mirrored into the legacy `conversations.stage`/`intercepted` columns, and the
-- in-process `DialogState` (app/core/state.py) never owns it either.

CREATE TABLE IF NOT EXISTS leads (
    id                          BIGSERIAL PRIMARY KEY,
    lead_status                 VARCHAR(32)  NOT NULL DEFAULT 'new',
    lead_source                 VARCHAR(32)  NOT NULL DEFAULT 'telegram_test',
    name                        VARCHAR(160) NOT NULL DEFAULT '',
    phone                       VARCHAR(64),
    telegram_username           VARCHAR(64),
    grade_base                  VARCHAR(32),
    direction                   VARCHAR(160),
    qualification               JSON         NOT NULL DEFAULT '{}',
    lead_temperature            VARCHAR(16)  NOT NULL DEFAULT 'new',
    suggested_status            VARCHAR(32),
    next_action_type            VARCHAR(32),
    next_action_at              TIMESTAMPTZ,
    ai_summary                  TEXT,
    escalation_reason           TEXT,
    manual_status_lock_until    TIMESTAMPTZ,
    status_change_source        VARCHAR(16),   -- bot|admin|trello|system
    status_change_by            VARCHAR(64),
    status_change_reason        TEXT,
    created_at                  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- lead_status is exactly one of: new, pre_contract, in_progress, contract, tested_thinking,
-- callback, thinking, invited, info_sent, rejected, invalid_number (enforced in application
-- code, not as a DB CHECK constraint, to keep this migration additive/low-risk).

CREATE INDEX IF NOT EXISTS ix_leads_lead_status ON leads (lead_status);
CREATE INDEX IF NOT EXISTS ix_leads_lead_source ON leads (lead_source);


CREATE TABLE IF NOT EXISTS pilot_conversations (
    id                  BIGSERIAL PRIMARY KEY,
    session_id          VARCHAR(64)  NOT NULL DEFAULT '',
    channel             VARCHAR(32)  NOT NULL DEFAULT 'telegram',
    bot_id              VARCHAR(64)  NOT NULL DEFAULT '',
    external_user_id    VARCHAR(160) NOT NULL DEFAULT '',
    external_chat_id    VARCHAR(160) NOT NULL DEFAULT '',
    lead_id             BIGINT       REFERENCES leads (id),
    bot_phase           VARCHAR(32)  NOT NULL DEFAULT 'greeting',  -- greeting|qualification|consultation|waiting|handoff
    dialog_owner        VARCHAR(16)  NOT NULL DEFAULT 'bot',       -- bot|manager|paused
    assigned_to         VARCHAR(64)  NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    archived_at         TIMESTAMPTZ                                -- NULL = active session
);

CREATE INDEX IF NOT EXISTS ix_pilot_conversations_session_id ON pilot_conversations (session_id);
CREATE INDEX IF NOT EXISTS ix_pilot_conversations_bot_user ON pilot_conversations (bot_id, external_user_id);
CREATE INDEX IF NOT EXISTS ix_pilot_conversations_lead_id ON pilot_conversations (lead_id);


-- ---------------------------------------------------------------------------------------
-- Rollback (manual only — NOT executed automatically by anything). Run these two
-- statements by hand if this increment must be reverted. They drop ONLY the two new
-- tables created above; the legacy `conversations`/`messages`/`deals`/`faq_entries`/etc.
-- tables are never touched by this migration or its rollback.
-- ---------------------------------------------------------------------------------------
-- DROP TABLE IF EXISTS pilot_conversations;
-- DROP TABLE IF EXISTS leads;

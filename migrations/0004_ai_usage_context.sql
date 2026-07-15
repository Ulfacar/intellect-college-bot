-- Migration 0004: AI usage + answer-context log (Telegram pilot, Increment 6).
--
-- There is NO Alembic in this project -- SQLAlchemy `Base.metadata.create_all()`
-- (app/integrations/crm/db.py::init_db/init_models) creates this same table on app
-- startup (additive, never drops), together with everything else on `Base`. This file
-- is a standalone, idempotent, manually-runnable equivalent for operators who prefer
-- plain SQL / CI migration steps.
--
-- Additive only:
--   - Creates ONE NEW table: `ai_answer_log`.
--   - Does NOT alter, rename or drop anything from migrations 0001/0002/0003 or any
--     legacy table. Does not touch `leads`/`pilot_conversations` beyond adding FK
--     references FROM this new table TO them.
--   - Safe to run multiple times (CREATE TABLE/INDEX IF NOT EXISTS).
--   - No secrets, no real college answers/conversations here -- this file ships no
--     seed data, and the application NEVER writes the full system prompt or raw model
--     output into this table (see app/core/ai_reply.py).
--
-- Why ONE table for both usage (brief §14) and answer-context (§15): a single
-- structured OpenRouter call produces both at once (see app/agent/structured_llm.py +
-- app/core/ai_reply.py) -- splitting them into two tables would just duplicate the
-- conversation_id/created_at join key for no benefit. See
-- app/integrations/panel/ai_log_store.py for the read/write contract and
-- app/core/budget.py for how `cost`/`created_at` here back the daily/monthly budget
-- gate (a `pending`-style row is inserted with `outcome='reserved'` BEFORE the network
-- call so concurrent requests see it in their own spend sum, then updated in place once
-- the call resolves -- see app/core/budget.py::reserve for the documented concurrency
-- approach, including the Postgres `pg_advisory_xact_lock` used to serialize
-- reservations across worker processes for the same UTC day).

CREATE TABLE IF NOT EXISTS ai_answer_log (
    id                     BIGSERIAL PRIMARY KEY,
    request_id             VARCHAR(64)  NOT NULL DEFAULT '',
    generation_id          VARCHAR(64),
    conversation_id        BIGINT REFERENCES pilot_conversations (id),
    lead_id                BIGINT REFERENCES leads (id),
    bot_id                 VARCHAR(64)  NOT NULL DEFAULT '',
    model                  VARCHAR(128) NOT NULL DEFAULT '',
    prompt_version         VARCHAR(32)  NOT NULL DEFAULT '',

    -- §14 usage/cost
    input_tokens           INTEGER,
    output_tokens          INTEGER,
    total_tokens           INTEGER,
    cached_tokens          INTEGER,
    cost                   DOUBLE PRECISION,
    -- provider | estimated (provider-reported OpenRouter usage.cost preferred; a
    -- rough per-1M-token estimate is used ONLY when the provider did not report one)
    cost_source            VARCHAR(16),
    latency_ms             DOUBLE PRECISION,
    -- reserved | sent | cancelled_by_takeover | validator_blocked | budget_exhausted |
    -- schema_error | timeout | connection | unauthorized | payment_required |
    -- http_error | error
    outcome                VARCHAR(24)  NOT NULL DEFAULT 'reserved',
    retry_count             INTEGER      NOT NULL DEFAULT 0,

    -- §15 answer-context
    client_message_id      VARCHAR(64),
    bot_message_id         VARCHAR(64),
    source                 VARCHAR(16)  NOT NULL DEFAULT 'llm',
    knowledge_entry_ids    JSON         NOT NULL DEFAULT '[]',
    language                VARCHAR(16),
    intent                  VARCHAR(32),
    confidence               DOUBLE PRECISION,
    evidence                 TEXT,
    suggested_status         VARCHAR(32),
    applied_status            VARCHAR(32),
    lead_temperature          VARCHAR(16),
    bot_phase                 VARCHAR(32),
    dialog_owner               VARCHAR(16),
    validator_violations       JSON     NOT NULL DEFAULT '[]',

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- conversation_id: read a conversation's AI answer history (admin "right panel").
CREATE INDEX IF NOT EXISTS ix_ai_answer_log_conversation_id ON ai_answer_log (conversation_id);
-- bot_id: per-bot budget/usage breakdown.
CREATE INDEX IF NOT EXISTS ix_ai_answer_log_bot_id ON ai_answer_log (bot_id);
-- created_at: period sums (any bot).
CREATE INDEX IF NOT EXISTS ix_ai_answer_log_created_at ON ai_answer_log (created_at);
-- (bot_id, created_at): the exact shape app/core/budget.py's daily/monthly sum query
-- needs (optionally filtered by bot_id) -- see the brief's explicit ask for a
-- "budget-relevant index on (bot_id, created_at) for period sums".
CREATE INDEX IF NOT EXISTS ix_ai_answer_log_bot_created ON ai_answer_log (bot_id, created_at);
CREATE INDEX IF NOT EXISTS ix_ai_answer_log_outcome ON ai_answer_log (outcome);

-- ---------------------------------------------------------------------------------------
-- Rollback (manual only -- NOT executed automatically by anything). Run this statement
-- by hand if this increment must be reverted. It drops ONLY the new table created
-- above; migrations 0001/0002/0003 and everything legacy are untouched.
-- ---------------------------------------------------------------------------------------
-- DROP TABLE IF EXISTS ai_answer_log;

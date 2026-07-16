-- Migration 0005: unified AnswerContext + Feedback (Telegram pilot, Increment 7).
--
-- There is NO Alembic in this project -- SQLAlchemy `Base.metadata.create_all()`
-- (app/integrations/crm/db.py::init_db/init_models) creates these same tables on app
-- startup (additive, never drops), together with everything else on `Base`. This file
-- is a standalone, idempotent, manually-runnable equivalent for operators who prefer
-- plain SQL / CI migration steps.
--
-- Additive only:
--   - Creates TWO NEW tables: `answer_context`, `feedback`.
--   - Does NOT alter, rename or drop anything from migrations 0001-0004 or any legacy
--     table. In particular `faq_kb_answer_log` and `ai_answer_log` are UNCHANGED and
--     keep being written exactly as before -- `answer_context` is the ADDITIONAL
--     canonical feedback anchor, not a replacement.
--   - Safe to run multiple times (CREATE TABLE/INDEX IF NOT EXISTS).
--   - No secrets, no real college answers/conversations here -- this file ships no
--     seed data. The application NEVER writes API keys, the full system prompt, the
--     webhook secret, or the raw BLOCKED model reply into `answer_context.reply_text`
--     (a validator_blocked answer stores the SAFE fallback text actually sent, plus
--     sanitized violation codes in `validator_violations` -- see
--     app/core/pilot_validator.py and app/core/ai_reply.py).
--
-- Why ONE additional table for every automatic answer (not per-source tables): a
-- `feedback_token` must exist BEFORE the reply is sent (it goes into the inline
-- keyboard's callback_data), and the callback handler must be able to resolve ANY
-- automatic answer (FAQ, LLM, any fallback) via that same token with one lookup --
-- see app/core/feedback_service.py.

CREATE TABLE IF NOT EXISTS answer_context (
    id                          BIGSERIAL PRIMARY KEY,
    conversation_id             BIGINT REFERENCES pilot_conversations (id),
    lead_id                     BIGINT REFERENCES leads (id),
    session_id                  VARCHAR(64)  NOT NULL DEFAULT '',
    bot_id                      VARCHAR(64)  NOT NULL DEFAULT '',
    channel                     VARCHAR(32)  NOT NULL DEFAULT 'telegram',
    -- client's Telegram user_id -- needed to re-verify a callback server-side (§7).
    telegram_tester_id          VARCHAR(64)  NOT NULL DEFAULT '',
    chat_id                     VARCHAR(64)  NOT NULL DEFAULT '',
    client_message_id           VARCHAR(64),
    provider_client_message_id  VARCHAR(64),
    bot_message_id              VARCHAR(64),
    provider_bot_message_id     VARCHAR(64),
    -- faq | llm | safe_fallback | handoff | budget_fallback | model_error_fallback
    source                      VARCHAR(24)  NOT NULL DEFAULT '',
    faq_entry_id                INTEGER,
    faq_version_id              INTEGER,
    matched_variant_id          INTEGER,
    match_type                  VARCHAR(16),
    match_score                 DOUBLE PRECISION,
    model                       VARCHAR(128),
    prompt_version               VARCHAR(32),
    knowledge_entry_ids          JSON         NOT NULL DEFAULT '[]',
    language                     VARCHAR(16),
    reply_text                   TEXT         NOT NULL DEFAULT '',
    intent                       VARCHAR(32),
    confidence                   DOUBLE PRECISION,
    evidence                     TEXT,
    suggested_status             VARCHAR(32),
    applied_status                VARCHAR(32),
    lead_temperature              VARCHAR(16),
    bot_phase                     VARCHAR(32),
    dialog_owner                   VARCHAR(16),
    validator_violations           JSON     NOT NULL DEFAULT '[]',
    input_tokens                    INTEGER,
    output_tokens                    INTEGER,
    total_tokens                      INTEGER,
    cost                               DOUBLE PRECISION,
    cost_source                         VARCHAR(16),
    latency_ms                           DOUBLE PRECISION,
    -- faq_answered | llm_answered | safe_fallback | validator_blocked | budget_fallback
    -- | model_error_fallback | handoff_only
    outcome                               VARCHAR(24)  NOT NULL DEFAULT '',
    -- short random urlsafe token (~12 chars) minted BEFORE send, used in callback_data
    -- as `fb:<feedback_token>:<code>` -- see app/channels/telegram.py.
    feedback_token                         VARCHAR(24)  NOT NULL DEFAULT '',
    created_at                              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_answer_context_feedback_token ON answer_context (feedback_token);
CREATE INDEX IF NOT EXISTS ix_answer_context_conversation_id ON answer_context (conversation_id);
CREATE INDEX IF NOT EXISTS ix_answer_context_bot_id ON answer_context (bot_id);
CREATE INDEX IF NOT EXISTS ix_answer_context_created_at ON answer_context (created_at);
CREATE INDEX IF NOT EXISTS ix_answer_context_outcome ON answer_context (outcome);

CREATE TABLE IF NOT EXISTS feedback (
    id                     BIGSERIAL PRIMARY KEY,
    answer_context_id      BIGINT NOT NULL REFERENCES answer_context (id),
    conversation_id        BIGINT REFERENCES pilot_conversations (id),
    lead_id                BIGINT REFERENCES leads (id),
    session_id             VARCHAR(64)  NOT NULL DEFAULT '',
    bot_id                 VARCHAR(64)  NOT NULL DEFAULT '',
    telegram_tester_id     VARCHAR(64)  NOT NULL DEFAULT '',
    -- correct | inaccurate | incorrect | should_push | should_not_push | should_handoff
    rating                 VARCHAR(24),
    comment                TEXT,
    expected_answer        TEXT,
    expected_intent        VARCHAR(32),
    expected_status        VARCHAR(32),
    expected_handoff       BOOLEAN,
    -- unreviewed | in_review | fixed | dismissed (SEPARATE axis from `rating`)
    review_status          VARCHAR(16)  NOT NULL DEFAULT 'unreviewed',
    reviewed_by            VARCHAR(64),
    reviewed_at            TIMESTAMPTZ,
    resolution_note        TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One tester rates one answer_context at most once -- a re-rating UPDATEs this row
-- (§8 idempotency); two concurrent callbacks for the same (answer_context, tester)
-- collapse to one row via this constraint + a single transaction.
CREATE UNIQUE INDEX IF NOT EXISTS ux_feedback_answer_context_tester ON feedback (answer_context_id, telegram_tester_id);
CREATE INDEX IF NOT EXISTS ix_feedback_answer_context_id ON feedback (answer_context_id);
CREATE INDEX IF NOT EXISTS ix_feedback_bot_id ON feedback (bot_id);
CREATE INDEX IF NOT EXISTS ix_feedback_telegram_tester_id ON feedback (telegram_tester_id);
CREATE INDEX IF NOT EXISTS ix_feedback_rating ON feedback (rating);
CREATE INDEX IF NOT EXISTS ix_feedback_review_status ON feedback (review_status);
CREATE INDEX IF NOT EXISTS ix_feedback_created_at ON feedback (created_at);

-- ---------------------------------------------------------------------------------------
-- Rollback (manual only -- NOT executed automatically by anything). Run these
-- statements by hand if this increment must be reverted. Drops ONLY the two new
-- tables created above; migrations 0001-0004 and everything legacy are untouched.
-- `feedback` is dropped first (it references `answer_context`).
-- ---------------------------------------------------------------------------------------
-- DROP TABLE IF EXISTS feedback;
-- DROP TABLE IF EXISTS answer_context;

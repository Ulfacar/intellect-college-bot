-- Migration 0006: split the single-axis `feedback.rating` into two INDEPENDENT axes
-- (Telegram pilot, Increment 7.1 -- corrective on top of Increment 7 / migration 0005).
--
-- There is NO Alembic in this project -- SQLAlchemy `Base.metadata.create_all()`
-- (app/integrations/crm/db.py::init_db/init_models) already creates `feedback` WITH
-- the two new columns on a BRAND-NEW database, because the ORM `Feedback` model now
-- carries `quality_rating`/`strategy_rating` directly. `create_all` only creates
-- tables that don't exist yet -- it never alters an EXISTING table -- so this file's
-- ALTER TABLE statements are the upgrade path for a Postgres deployment that already
-- ran migration 0005 (and therefore already has a `feedback` table) BEFORE this
-- increment. On SQLite test databases (always created fresh per test) and on any
-- brand-new Postgres install, these two ALTERs are no-ops. `app/integrations/crm/
-- db.py::init_models` also runs the equivalent idempotent `_ensure_columns` +
-- `backfill_feedback_rating_axes` on every app startup, so an operator does not have
-- to remember to run this file manually -- it exists for operators who prefer plain
-- SQL / CI migration steps, same convention as migrations 0001-0005.
--
-- Additive + reversible:
--   - Adds TWO NEW nullable columns to the EXISTING `feedback` table.
--   - Does NOT alter, rename or drop `rating` (or anything else) -- `rating` is KEPT
--     as a legacy, read-only-after-backfill column; new code never writes it again
--     (see app/integrations/panel/feedback_store.py::set_axis_rating). `review_status`
--     is untouched, stays a separate axis exactly as before.
--   - Safe to run multiple times (IF NOT EXISTS everywhere; the backfill UPDATEs only
--     ever touch a column that is CURRENTLY NULL, so re-running never clobbers a value
--     a tester has since set via a real button tap).
--   - No secrets, no real college data -- this file ships no seed data.

ALTER TABLE feedback ADD COLUMN IF NOT EXISTS quality_rating  VARCHAR(24);
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS strategy_rating VARCHAR(24);

CREATE INDEX IF NOT EXISTS ix_feedback_quality_rating  ON feedback (quality_rating);
CREATE INDEX IF NOT EXISTS ix_feedback_strategy_rating ON feedback (strategy_rating);

-- Idempotent backfill: map the legacy single-axis `rating` onto whichever of the two
-- NEW axes it belongs to (mirrored statement-for-statement in Python as
-- app/integrations/crm/db.py::backfill_feedback_rating_axes, exercised against SQLite
-- in tests/test_feedback_two_axis.py so the SAME mapping is verified there and here):
--   correct | inaccurate | incorrect                 -> quality_rating
--   should_push | should_not_push | should_handoff    -> strategy_rating
-- `appropriate` (the new "conversation was pushed at exactly the right level" value)
-- has NO legacy source -- it stays NULL for every pre-existing row; it can only ever
-- be set by a tester tapping the NEW "👍 Ведение верное" button going forward.
UPDATE feedback
SET quality_rating = rating
WHERE quality_rating IS NULL
  AND rating IN ('correct', 'inaccurate', 'incorrect');

UPDATE feedback
SET strategy_rating = rating
WHERE strategy_rating IS NULL
  AND rating IN ('should_push', 'should_not_push', 'should_handoff');

-- ---------------------------------------------------------------------------------------
-- Rollback (manual only -- NOT executed automatically by anything). Drops ONLY the two
-- NEW columns (and their indexes) added above. `rating`, `review_status`, and
-- everything from migrations 0001-0005 stays untouched -- a rollback here does not
-- lose any tester feedback, it only removes the two-axis split (the legacy `rating`
-- values that were backfilled remain readable in `rating` itself).
-- ---------------------------------------------------------------------------------------
-- DROP INDEX IF EXISTS ix_feedback_strategy_rating;
-- DROP INDEX IF EXISTS ix_feedback_quality_rating;
-- ALTER TABLE feedback DROP COLUMN IF EXISTS strategy_rating;
-- ALTER TABLE feedback DROP COLUMN IF EXISTS quality_rating;

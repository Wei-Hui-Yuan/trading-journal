-- Migration 007: single source of truth for qualitative review
--
-- Review state moves from the execution ledger (`trades`) to the round-trip
-- level (`positions`). A trade could previously be "reviewed" in the Analytics
-- tab while the same round trip sat "pending" in the Trade Inbox, with nothing
-- reconciling the two.
--
-- STATUS VOCABULARY: `positions.review_status` previously terminated at
-- 'completed' (Trade Inbox) while the trade-level flow used 'reviewed'. Both
-- now terminate at 'reviewed' so a single `review_status = 'pending'` filter
-- drives every queue. Existing 'completed' rows are migrated below.
--
-- DESTRUCTIVE: this drops three columns from `trades`. Verified empty before
-- running -- no row carried notes, mistakes, or a non-default review_status.

-- ---------------------------------------------------------------------------
-- 1. positions gains the qualitative fields
-- ---------------------------------------------------------------------------

ALTER TABLE positions
    ADD COLUMN IF NOT EXISTS review_status TEXT DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS notes         TEXT,
    ADD COLUMN IF NOT EXISTS mistakes      TEXT[] DEFAULT '{}';

-- Rows predating this migration would otherwise hold NULLs, which the
-- `review_status = 'pending'` queue filter silently excludes.
UPDATE positions SET review_status = 'pending' WHERE review_status IS NULL;
UPDATE positions SET mistakes      = '{}'      WHERE mistakes IS NULL;

-- Unify the terminal status so both UIs agree on what "done" means.
UPDATE positions SET review_status = 'reviewed' WHERE review_status = 'completed';

-- ---------------------------------------------------------------------------
-- 2. Indexes
-- ---------------------------------------------------------------------------

-- Every queue load filters on this column.
CREATE INDEX IF NOT EXISTS idx_positions_review_status ON positions (review_status);

-- GIN makes "performance grouped by mistake tag" a matching-row scan rather
-- than a full scan with array unnesting.
CREATE INDEX IF NOT EXISTS idx_positions_mistakes ON positions USING GIN (mistakes);

-- ---------------------------------------------------------------------------
-- 3. Remove the duplicated columns from trades
-- ---------------------------------------------------------------------------
-- `trades` keeps `status` (execution lifecycle) and `lessons_comments`; only
-- the review-duplication columns added in migration 006 are removed.

DROP INDEX IF EXISTS idx_trades_review_status;
DROP INDEX IF EXISTS idx_trades_mistakes;

ALTER TABLE trades
    DROP COLUMN IF EXISTS review_status,
    DROP COLUMN IF EXISTS notes,
    DROP COLUMN IF EXISTS mistakes;

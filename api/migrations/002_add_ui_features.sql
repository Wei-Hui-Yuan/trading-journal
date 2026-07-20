-- Migration 002: UI feature support
--
-- Backs the Strategies engine, the Day/Session heatmap, and the Trade Inbox
-- review checklist.
--
-- NOTE ON `strategies`: this table already exists from the original schema and
-- holds live rows, so it is NOT recreated here. The CREATE is guarded with
-- IF NOT EXISTS (a no-op on this database) purely so the migration can also
-- bootstrap a fresh environment. The `name` column is widened 50 -> 100 in
-- place, and the pre-existing `instruments TEXT[]` column is left untouched.
--
-- Run in the Supabase SQL Editor, or via the project's migration runner.

CREATE TABLE IF NOT EXISTS strategies (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Widen name on databases where the table predates this migration.
-- Widening is non-destructive; existing values are preserved.
ALTER TABLE strategies
    ALTER COLUMN name TYPE VARCHAR(100);


-- ---------------------------------------------------------------------------
-- positions: review workflow + strategy attribution
-- ---------------------------------------------------------------------------
-- All columns are nullable or defaulted so existing rows remain valid.

ALTER TABLE positions
    ADD COLUMN IF NOT EXISTS strategy_id        UUID REFERENCES strategies(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS review_status      VARCHAR(20) DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS tag_hard_sl        BOOLEAN     DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS tag_retest         BOOLEAN     DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS tag_plan_compliant BOOLEAN     DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS trade_grade        VARCHAR(5);

-- The Trade Inbox filters on review_status; the heatmap scans entry_time.
CREATE INDEX IF NOT EXISTS idx_positions_review_status ON positions (review_status);
CREATE INDEX IF NOT EXISTS idx_positions_entry_time    ON positions (entry_time);
CREATE INDEX IF NOT EXISTS idx_positions_strategy_id   ON positions (strategy_id);

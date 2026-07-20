-- Migration 006: qualitative review fields on the trades ledger
--
-- Backs the Analytics & Review tab: a per-execution review pass capturing
-- free-form notes and multi-tagged behavioural mistakes.
--
-- NOTE ON OVERLAP -- `trades` already carries two adjacent columns:
--   * `status`           'pending_review' | 'completed'  (execution lifecycle)
--   * `lessons_comments` TEXT                            (free-form reflection)
-- The new `review_status` / `notes` sit alongside them rather than replacing
-- them, so nothing that reads the old columns breaks. See the note in the
-- Phase 5 summary about consolidating these.
--
-- All columns are defaulted or nullable so broker-synced fills and existing
-- rows stay valid.

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS review_status TEXT DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS notes         TEXT,
    ADD COLUMN IF NOT EXISTS mistakes      TEXT[] DEFAULT '{}';

-- Rows that predate this migration would otherwise carry NULLs, which the
-- pending-queue filter (`review_status = 'pending'`) would silently exclude.
UPDATE trades SET review_status = 'pending' WHERE review_status IS NULL;
UPDATE trades SET mistakes = '{}'           WHERE mistakes IS NULL;

-- The review queue filters on this column on every page load.
CREATE INDEX IF NOT EXISTS idx_trades_review_status ON trades (review_status);

-- GIN index makes "performance grouped by mistake tag" a scan of matching
-- rows rather than a full table scan with array unnesting.
CREATE INDEX IF NOT EXISTS idx_trades_mistakes ON trades USING GIN (mistakes);

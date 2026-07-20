-- Migration 008: remove the last of the legacy trade-level review model
--
-- Drops two columns from `trades`:
--
--   * `lessons_comments` -- superseded by `positions.notes` (migration 007).
--     Verified empty before running: no row carried a value.
--
--   * `status` -- the old 'pending_review' | 'completed' execution lifecycle.
--     Review state now lives solely on `positions.review_status`, and the two
--     endpoints that read this column (GET /api/trades, which grouped its
--     response by status, and PUT /api/trades/{id}, the old trade-level
--     "complete" flow) are removed in the same change. Neither had a frontend
--     caller.
--
-- Checked before executing: no views and no triggers depend on `trades`.
--
-- DESTRUCTIVE and not reversible by re-running -- the column data is gone.
-- `trades` remains the immutable execution ledger; what a fill WAS (price,
-- quantity, direction, timestamps) and what was PLANNED (planned_entry,
-- stop_loss, target) both stay.

ALTER TABLE trades
    DROP COLUMN IF EXISTS status,
    DROP COLUMN IF EXISTS lessons_comments;

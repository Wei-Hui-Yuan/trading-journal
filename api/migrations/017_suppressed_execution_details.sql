-- Migration 017: remember WHAT was suppressed, not just that something was.
--
-- Migration 016 stored only the broker id, the ticker and a reason. That is
-- enough to stop a sync re-adding a fill, which was all it had to do. It is
-- not enough to review the decision later: a list reading
-- "IBKR-111634108 · CAT · deleted from the journal" cannot answer the only
-- question that matters when un-suppressing -- "which fill was this?".
--
-- The row it names is deleted, so these values cannot be joined back from
-- `trades`. They have to be copied at tombstone time or they are gone.
--
-- All nullable. Rows written by migration 016's code have no way to backfill
-- these, and a NULL that reads as "not recorded" is honest where a zero
-- quantity or a 1970 date would be an invention.

ALTER TABLE suppressed_executions ADD COLUMN IF NOT EXISTS direction   TEXT;
ALTER TABLE suppressed_executions ADD COLUMN IF NOT EXISTS quantity    NUMERIC(18, 8);
ALTER TABLE suppressed_executions ADD COLUMN IF NOT EXISTS price       NUMERIC(10, 4);
ALTER TABLE suppressed_executions ADD COLUMN IF NOT EXISTS executed_at TIMESTAMPTZ;

-- The management list is ordered by when the fill happened, not by when it was
-- suppressed: the user is looking for a trade they remember taking.
CREATE INDEX IF NOT EXISTS ix_suppressed_executions_executed_at
    ON suppressed_executions (executed_at DESC);

-- Migration 005: IBKR raw execution staging ledger
--
-- Every fill IBKR reports lands here first, keyed by the broker's own
-- transaction id. That UNIQUE constraint is the idempotency guarantee: a
-- re-sync of an overlapping date range collides and is skipped, so the same
-- fill can never reach the trades ledger twice.
--
-- `processed` marks rows that have been promoted into `trades`, so a partial
-- failure mid-ingest can be resumed without re-reading the broker.
--
-- NOTE ON `quantity`: stored SIGNED, exactly as IBKR reports it (positive =
-- bought, negative = sold). The sign is the only record of the side in this
-- table, and promotion to `trades` derives direction from it and stores the
-- absolute value.

CREATE TABLE IF NOT EXISTS ibkr_executions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id TEXT NOT NULL UNIQUE,
    symbol         TEXT NOT NULL,
    quantity       INTEGER NOT NULL,
    price          NUMERIC(14, 6),
    commission     NUMERIC(14, 6),
    execution_time TIMESTAMPTZ,
    processed      BOOLEAN DEFAULT FALSE,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Resume scans look for unprocessed rows; symbol lookups drive FIFO re-runs.
CREATE INDEX IF NOT EXISTS idx_ibkr_exec_processed ON ibkr_executions (processed);
CREATE INDEX IF NOT EXISTS idx_ibkr_exec_symbol    ON ibkr_executions (symbol);

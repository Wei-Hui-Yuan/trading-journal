-- Migration 001: positions table
--
-- Clean-room separation: the `trades` table stays an immutable log of raw
-- broker executions. Round trips computed by the FIFO matching engine land
-- here instead, so matching never mutates source data.
--
-- Run in the Supabase SQL Editor (Dashboard -> SQL Editor -> New query).

CREATE TABLE IF NOT EXISTS positions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol        VARCHAR(20)     NOT NULL,
    style         VARCHAR(50)     NOT NULL,  -- Scalp | Day Trade | Swing Trade
    quantity      NUMERIC(12, 4)  NOT NULL,
    entry_price   NUMERIC(10, 4)  NOT NULL,
    exit_price    NUMERIC(10, 4)  NOT NULL,
    entry_time    TIMESTAMPTZ     NOT NULL,
    exit_time     TIMESTAMPTZ     NOT NULL,
    realized_pnl  NUMERIC(12, 4)  NOT NULL,
    created_at    TIMESTAMPTZ     DEFAULT NOW()
);

-- Dashboard reads group by symbol and scan by close time.
CREATE INDEX IF NOT EXISTS idx_positions_symbol    ON positions (symbol);
CREATE INDEX IF NOT EXISTS idx_positions_exit_time ON positions (exit_time DESC);


-- ---------------------------------------------------------------------------
-- Idempotency guard
-- ---------------------------------------------------------------------------
-- Without a link back to the executions a position was derived from, there is
-- no way to tell what has already been matched, so re-running the engine would
-- insert duplicate rows every time.
--
-- These two columns record which pair of executions produced each position,
-- and the unique index below turns a re-run into a no-op: the matching engine
-- issues its INSERT with ON CONFLICT DO NOTHING, which catches the violation.
--
-- NOTE: the engine must populate both columns for this to work. Postgres
-- treats NULLs as distinct in a unique index, so rows with (NULL, NULL) would
-- never collide and the guard would silently do nothing.

ALTER TABLE positions
    ADD COLUMN IF NOT EXISTS open_trade_id  UUID REFERENCES trades(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS close_trade_id UUID REFERENCES trades(id) ON DELETE SET NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_positions_open_close
    ON positions (open_trade_id, close_trade_id);

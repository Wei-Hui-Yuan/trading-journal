-- Migration 022: record realised P&L where it actually happens -- the closing fill
--
-- THE BUG THIS FIXES
--
-- `positions` gets a row only when a ticker returns to FLAT. That is the right
-- grain for counting ideas: scaling in and out is one trade, and one row keeps
-- trade count and win rate honest. It is the wrong grain for money.
--
-- Money is realised by a closing FILL, not by a round trip. Sell half a
-- position and that P&L is banked, whatever happens to the other half. Because
-- no row existed until the run went flat, every dollar realised on the way out
-- of a position still held was recorded NOWHERE.
--
-- Measured on this account, 2026 year-to-date:
--
--   MSFT  scaled in and out five times since 29 May, still holds 2 shares
--         -> -63.34 gross realised, no row
--   VRT   bought 2 on 22 Jun, sold 1 on 25 Jun, still holds 1
--         ->  -9.00 gross realised, no row
--
-- The dashboard reported +66.20 year-to-date gross. The broker's own fills say
-- -6.14. The whole 72.34 difference is those two runs. The all-time figure was
-- wrong by the same money: -285.69 reported against -361.64 actual.
--
-- The matching engine had already computed every one of these legs and was
-- throwing them away -- `MatchingResult.open_round_trip_realized_pnl` summed
-- them, was asserted in tests, and was read by nothing else.
--
-- A SECOND BUG, FIXED BY THE SAME TABLE
--
-- A round trip's entire P&L was dated by its FINAL exit. Scale out in December,
-- close in January, and every dollar landed in January. That contributes zero
-- on today's data by luck -- the three round trips spanning 2025/2026 each had
-- a single exit -- but it silently corrupts any windowed figure the first time
-- a scale-out straddles a boundary, which is exactly what the new timeframe
-- filter invites.
--
-- WHAT GOES WHERE, AFTER THIS
--
--   MONEY  -- net P&L, gross, commission, the equity curve, drawdown, and
--             every windowed total -- sums `realized_legs` by exit_time.
--   COUNTS -- trade count, win rate, profit factor, ROI, grades, reviews --
--             stay on `positions`, one row per completed idea.
--
-- So a partial exit moves the money and does not count as a trade, which is
-- the distinction the flat-to-flat design was protecting in the first place.
--
-- PURELY DERIVED. Unlike `positions`, which carries the trader's review, every
-- column here is computed from `trades`. Nothing a user typed is stored, so a
-- rebuild replaces a ticker's legs outright rather than reconciling them --
-- there is nothing to preserve and therefore nothing to get wrong.

BEGIN;

CREATE TABLE IF NOT EXISTS realized_legs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    symbol          VARCHAR(20)   NOT NULL,
    -- LONG or SHORT, as the lot was held. Recorded because it is known here
    -- and cannot be recovered later: `positions` does not store direction, and
    -- an open run has no positions row to ask.
    direction       VARCHAR(5)    NOT NULL,
    quantity        NUMERIC(18, 8) NOT NULL,

    -- The pair of executions this slice pairs off. FIFO consumes a lot once,
    -- so a given (open, close) pair names exactly one leg -- which is what
    -- makes the unique index below a safe idempotency key.
    open_trade_id   UUID NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    close_trade_id  UUID NOT NULL REFERENCES trades(id) ON DELETE CASCADE,

    entry_price     NUMERIC(10, 4) NOT NULL,
    exit_price      NUMERIC(10, 4) NOT NULL,
    entry_time      TIMESTAMPTZ    NOT NULL,
    -- WHEN THE MONEY WAS REALISED. This column is the entire point of the
    -- table: every period figure buckets on it, so a window can no longer
    -- inherit a round trip's closing date for P&L banked months earlier.
    exit_time       TIMESTAMPTZ    NOT NULL,

    gross_pnl       NUMERIC(14, 4) NOT NULL,
    -- Apportioned by the fraction of each fill this leg consumed. Positive is
    -- money paid; negative is a rebate, which IBKR really does pay.
    commission      NUMERIC(14, 4) NOT NULL DEFAULT 0,
    realized_pnl    NUMERIC(14, 4) NOT NULL,

    -- The completed round trip this leg belongs to, or NULL when the run is
    -- still open -- money banked, idea not finished. That NULL is a fact worth
    -- querying, not a gap: it is precisely the P&L that had no home before.
    --
    -- SET NULL rather than CASCADE. Deleting a round trip must not delete the
    -- record that its money was realised; the rebuild that follows will
    -- re-derive both from the fills either way.
    position_id     UUID REFERENCES positions(id) ON DELETE SET NULL,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT realized_legs_quantity_positive CHECK (quantity > 0),
    CONSTRAINT realized_legs_direction_check   CHECK (direction IN ('LONG', 'SHORT'))
);

-- Re-running the matcher over the same fills must not duplicate a leg.
CREATE UNIQUE INDEX IF NOT EXISTS uq_realized_legs_open_close
    ON realized_legs (open_trade_id, close_trade_id);

-- Every windowed figure scans this range. The single most important index here.
CREATE INDEX IF NOT EXISTS ix_realized_legs_exit_time
    ON realized_legs (exit_time);

-- Rebuilds delete and re-insert one ticker at a time.
CREATE INDEX IF NOT EXISTS ix_realized_legs_symbol
    ON realized_legs (symbol);

-- "What have I banked out of positions I still hold?" -- the question that had
-- no answer before this table existed.
CREATE INDEX IF NOT EXISTS ix_realized_legs_open_runs
    ON realized_legs (exit_time)
    WHERE position_id IS NULL;

COMMENT ON TABLE realized_legs IS
    'Realised P&L per closing fill. Money buckets here by exit_time; counts '
    'and win rate stay on `positions`. Purely derived from `trades` -- safe to '
    'rebuild for any ticker at any time.';

COMMIT;

-- ---------------------------------------------------------------------------
-- BACKFILL
-- ---------------------------------------------------------------------------
-- Deliberately NOT attempted in SQL. FIFO lot matching with long/short flips
-- and per-leg commission apportionment is the matching engine's job, and a
-- second implementation in PL/pgSQL would be a second thing to keep correct.
--
-- After applying this, populate the table with:
--
--     POST /api/rematch          (every ticker; ~4s on 88 symbols)
--
-- which is idempotent and rebuilds legs and positions together from the fills.

-- Migration 009: link each round trip to the executions that composed it.
--
-- The matching engine now aggregates flat-to-flat: scaling in or out yields a
-- single row in `positions` rather than one per FIFO pairing, so trade count
-- and win rate stop counting one idea several times. That aggregation would
-- otherwise discard which fills produced the position, which is exactly the
-- detail slippage analysis and execution review need.
--
-- `quantity` is the share count attributed to THIS position, not the fill's
-- full size. They differ when one execution spans two round trips: an oversell
-- closes a long and opens a short with the same fill.
--
-- `trades` stays immutable -- this table references it and never writes to it.

CREATE TABLE IF NOT EXISTS position_fills (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    position_id  UUID NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
    trade_id     UUID NOT NULL REFERENCES trades(id)    ON DELETE CASCADE,
    role         VARCHAR(5) NOT NULL CHECK (role IN ('OPEN', 'CLOSE')),
    quantity     INTEGER NOT NULL CHECK (quantity > 0),
    price        NUMERIC(10, 4) NOT NULL,
    executed_at  TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Re-running the matcher over the same fills must not duplicate rows. A fill
-- can appear once per role within a position (closing one leg and opening the
-- next), so role is part of the key.
CREATE UNIQUE INDEX IF NOT EXISTS uq_position_fills_position_trade_role
    ON position_fills (position_id, trade_id, role);

-- The drill-down reads every fill for one position.
CREATE INDEX IF NOT EXISTS ix_position_fills_position
    ON position_fills (position_id);

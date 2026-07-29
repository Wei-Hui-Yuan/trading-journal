-- Migration 012: the plan, and the post-mortem of the plan.
--
-- The analytics engine already computes R-multiple, slippage and expectancy,
-- but 12 of 13 round trips scored as NULL: `compute_r_multiple` needs a stop
-- to divide by, and only one trade in the whole ledger had one. The metrics
-- were never broken, they were starved. There was simply nowhere in the UI to
-- record a plan. These columns are that missing input.
--
-- WHERE EACH FIELD LIVES, AND WHY
--
-- The plan belongs to the round trip, not to the execution -- but a round trip
-- that is still open has no `positions` row yet, so plan fields go on `trades`
-- and are read from the *opening* execution. That is already precisely what
-- `load_reviewed_trades` does, so analytics needs no rewiring, and an open
-- position can carry a plan the moment it is entered.
--
-- Scaling in makes this concrete: two CRWD buys at 09:42 are one trade idea
-- with one stop. Storing the stop per execution would allow two contradictory
-- stops and no way to say which was meant. `positions.open_trade_id` names the
-- one execution that carries the plan.
--
-- The review belongs on `positions`, because it can only be written once the
-- round trip is closed and its outcome known.
--
--   trades.actual_stop_loss   where the stop ACTUALLY sat, after any moves.
--                             Kept separate from stop_loss (the plan) so that
--                             "I widened my stop" is visible rather than
--                             silently overwriting the original intent.
--   trades.risk_amount        currency at risk. risk_percent already exists;
--                             this is the absolute figure, which is what turns
--                             an R-multiple back into money.
--   trades.conviction         1-5, rated at entry. Correlating conviction
--                             against realised R is how overconfidence shows
--                             up as a number instead of a feeling.
--   trades.emotional_state    free text at entry, pairs with mistake tags.
--
--   positions.exit_reason     target / stop / manual / trailing / time. The
--                             cheapest field that exposes cutting winners
--                             early while letting losers run to the stop.
--
-- Two families of hindsight levels, deliberately not merged:
--
--   positions.ideal_*         what the levels SHOULD have been on this trade,
--                             judged from the chart afterwards. Scores plan
--                             quality separately from execution quality: a
--                             good plan executed badly and a bad plan executed
--                             well produce the same P&L and need opposite
--                             fixes.
--   positions.revised_*       the corrected rule to apply to the NEXT instance
--                             of this setup. Aggregates by strategy and feeds
--                             the playbook, rather than scoring this trade.

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS actual_stop_loss NUMERIC(10, 4),
    ADD COLUMN IF NOT EXISTS risk_amount      NUMERIC(12, 2),
    ADD COLUMN IF NOT EXISTS conviction       SMALLINT,
    ADD COLUMN IF NOT EXISTS emotional_state  TEXT;

-- Conviction is a 1-5 scale. Enforced in the database as well as the API so a
-- direct SQL edit cannot quietly poison the conviction-vs-R correlation.
-- NULL stays allowed: unrated is a legitimate state and must not read as 1.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'trades_conviction_range'
    ) THEN
        ALTER TABLE trades
            ADD CONSTRAINT trades_conviction_range
            CHECK (conviction IS NULL OR conviction BETWEEN 1 AND 5);
    END IF;
END $$;

ALTER TABLE positions
    ADD COLUMN IF NOT EXISTS exit_reason    TEXT,
    ADD COLUMN IF NOT EXISTS ideal_entry    NUMERIC(10, 4),
    ADD COLUMN IF NOT EXISTS ideal_stop     NUMERIC(10, 4),
    ADD COLUMN IF NOT EXISTS ideal_target   NUMERIC(10, 4),
    ADD COLUMN IF NOT EXISTS revised_entry  NUMERIC(10, 4),
    ADD COLUMN IF NOT EXISTS revised_stop   NUMERIC(10, 4),
    ADD COLUMN IF NOT EXISTS revised_target NUMERIC(10, 4);

-- The journal groups closed round trips by their opening execution and lists
-- open exposure by ticker; both paths hit these columns on every page load.
CREATE INDEX IF NOT EXISTS ix_positions_open_trade_id ON positions (open_trade_id);
-- SUPERSEDED by migration 021, which drops this. Migration 009 had already
-- created ix_position_fills_position over the same column, and both were
-- redundant with uq_position_fills_position_trade_role, whose leading column
-- is position_id.
CREATE INDEX IF NOT EXISTS ix_position_fills_position_id ON position_fills (position_id);

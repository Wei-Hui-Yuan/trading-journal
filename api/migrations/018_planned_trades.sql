-- Migration 018: separate what you INTENDED to trade from what you actually did.
--
-- Until now `trades` accepted rows from two independent sources -- the IBKR
-- Flex ingest and hand entry -- with no shared identifier between them.
-- Deduplication keys on `ibkr_exec_id`, so a hand-logged fill (MANUAL-<uuid>)
-- and the broker's copy of the same execution (IBKR-<transaction_id>) could
-- never recognise each other. Logging a trade by hand and then syncing
-- produced two rows for one real execution: double the position, wrong average
-- cost, and phantom shares left open after the real ones were sold.
--
-- Content matching cannot close this. IBKR splits one order into several
-- executions -- this account has a position that arrived as 11 separate fills,
-- and 49 of 265 ticker/side/day groups are multi-fill -- so a single
-- hand-logged row has no quantity to match against. Tightening the comparison
-- instead merges genuine scale-ins, which loses a real fill: a worse failure,
-- and a quieter one.
--
-- So the fix is structural rather than statistical. A plan is not an
-- execution, and it now lives in its own table. `trades` holds executions
-- only, which makes the collision unrepresentable rather than unlikely.

CREATE TABLE IF NOT EXISTS planned_trades (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Widths match `trades` exactly, because attaching copies these values
    -- across. A plan able to hold a price the ledger cannot store would fail
    -- at attach time -- the one moment where failing is most expensive.
    ticker          VARCHAR(10)  NOT NULL,
    direction       VARCHAR(5)   NOT NULL,
    quantity        NUMERIC(18, 8),

    planned_entry   NUMERIC(10, 4),
    stop_loss       NUMERIC(10, 4),
    take_profit     NUMERIC(10, 4),

    -- Derived, and derived by the database so it cannot drift. Storing this as
    -- an ordinary column meant an edit to the stop had to remember to
    -- recompute it; whoever forgot once left a plan whose stated R disagreed
    -- with its own three inputs forever.
    --
    -- NULLIF turns an entry equal to the stop into NULL rather than a division
    -- error, and any NULL input propagates, so an incomplete plan simply has
    -- no R instead of a fabricated one.
    --
    -- The formula needs no direction branch: for a short, entry 100 / stop 105
    -- / target 90 gives (90-100)/(100-105) = 2R. Both signs flip and cancel.
    --
    -- NUMERIC(12,2) rather than (8,2) purely for headroom -- a stop a fraction
    -- of a cent from the entry yields an enormous ratio, and an overflow here
    -- would reject the whole INSERT.
    planned_r       NUMERIC(12, 2) GENERATED ALWAYS AS (
                        round(
                            (take_profit - planned_entry)
                            / NULLIF(planned_entry - stop_loss, 0),
                            2
                        )
                    ) STORED,

    -- What the position-size calculator worked out. Recorded per plan rather
    -- than recomputed later, for the same reason `trades` does it: account
    -- size drifts, and a plan sized against $2,500 must keep reading as a
    -- percentage of $2,500.
    risk_percent    NUMERIC(6, 2),
    risk_amount     NUMERIC(12, 2),

    -- A real reference, not free text: the playbook already exists, and a
    -- typed name would let a plan cite a strategy the journal cannot resolve.
    strategy_id     UUID REFERENCES strategies(id) ON DELETE SET NULL,
    thesis          TEXT,

    status          VARCHAR(20) NOT NULL DEFAULT 'OPEN',

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Enumerations enforced here rather than described in a comment. The
    -- dock filters on exact strings, so one 'Cancelled' or 'open' written by
    -- a future caller would silently empty the list instead of failing loudly.
    CONSTRAINT planned_trades_direction_check
        CHECK (direction IN ('BUY', 'SELL')),
    CONSTRAINT planned_trades_status_check
        CHECK (status IN ('OPEN', 'ATTACHED', 'CANCELLED'))
);

-- The auto-attach lookup on every sync: open plans for one ticker and side.
CREATE INDEX IF NOT EXISTS ix_planned_trades_lookup
    ON planned_trades (status, ticker, direction);

-- The dock, newest first.
CREATE INDEX IF NOT EXISTS ix_planned_trades_created_at
    ON planned_trades (created_at DESC);

-- Which plan a fill came from.
--
-- Deliberately one-directional. An `attached_trade_id` pointing back would be
-- the same fact stored twice and free to disagree the first time a detach
-- updated one side and forgot the other -- and it could not represent the
-- truth anyway, since one plan attaches to every fill of its opening leg.
-- The reverse lookup is a query: SELECT id FROM trades WHERE plan_id = ?
--
-- ON DELETE SET NULL, never CASCADE: deleting a plan must not delete
-- executions that really happened.
ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS plan_id UUID REFERENCES planned_trades(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_trades_plan_id ON trades (plan_id);

-- One live plan that was only ever a plan.
--
-- PANW, entered 2026-07-22 09:19 ET: planned entry 336.00, stop 328.60,
-- target 345.00, 3 shares, $22.20 at risk. Its `actual_entry` reads 345.00 --
-- identical to the target -- because the old form REQUIRED an execution price
-- to save anything, so planning a trade meant inventing a fill for it. The
-- ledger has been carrying it as an open 3-share position that was never
-- bought, and the next sync would have added the broker's copy beside it.
--
-- Moved rather than deleted: the thesis and sizing are the user's work.
-- Confirmed with them before running.
INSERT INTO planned_trades (
    ticker, direction, quantity, planned_entry, stop_loss, take_profit,
    risk_percent, risk_amount, strategy_id, thesis, status, created_at, updated_at
)
SELECT
    t.ticker, t.direction, t.quantity, t.planned_entry, t.stop_loss, t.target,
    t.risk_percent, t.risk_amount, t.strategy_id, t.thesis, 'OPEN',
    -- The moment it was written, which is what the attach window measures from.
    t.entry_date, NOW()
FROM trades t
WHERE t.ibkr_exec_id LIKE 'MANUAL-%'
  -- A hand-logged row already matched into a round trip describes something
  -- that demonstrably happened, whatever its prefix says. Left alone.
  AND NOT EXISTS (SELECT 1 FROM position_fills pf WHERE pf.trade_id = t.id);

DELETE FROM trades t
WHERE t.ibkr_exec_id LIKE 'MANUAL-%'
  AND NOT EXISTS (SELECT 1 FROM position_fills pf WHERE pf.trade_id = t.id);

-- Any MANUAL- row that survived the move is a real execution, so it is re-keyed
-- rather than removed. REPAIR- names what that path is actually for now: adding
-- a fill IBKR dropped, from inside a position you can see is short one. It is
-- no longer a general hand-logging surface, which is what made duplication
-- possible in the first place.
UPDATE trades
   SET ibkr_exec_id = 'REPAIR-' || substring(ibkr_exec_id from 8)
 WHERE ibkr_exec_id LIKE 'MANUAL-%';

-- The guarantee, in the schema rather than in a convention.
--
-- Every row in `trades` is now either the broker's (IBKR-) or a deliberate
-- repair to a position that already exists (REPAIR-). There is no third kind,
-- and no code path can quietly introduce one.
--
-- `IS NOT NULL` is part of the test on purpose: a bare LIKE evaluates to NULL
-- for a NULL id, and a CHECK passes on NULL, which would leave exactly the
-- hole this constraint exists to close. Every one of the 327 existing rows
-- carries an id, so requiring it costs nothing today.
-- Wrapped because ADD CONSTRAINT has no IF NOT EXISTS, and a migration that
-- cannot be run twice is a migration you are afraid to re-run.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'trades_exec_id_source_check'
    ) THEN
        ALTER TABLE trades
            ADD CONSTRAINT trades_exec_id_source_check
            CHECK (
                ibkr_exec_id IS NOT NULL
                AND (ibkr_exec_id LIKE 'IBKR-%' OR ibkr_exec_id LIKE 'REPAIR-%')
            );
    END IF;
END $$;

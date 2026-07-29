-- Migration 023: carry IBKR's own realised P&L, and reconcile the fees to it
--
-- WHY
--
-- Our FIFO engine agrees with IBKR's lot matching exactly. Compared fill by
-- fill across the whole ledger, all 166 closing fills land within $0.50 of the
-- broker's own figure and the total gap is $9.14 over a year. There is no
-- structural error to find.
--
-- The gap is cost, not arithmetic. `ibCommission` is the commission alone;
-- IBKR's realised P&L also nets exchange, clearing and regulatory charges,
-- which the Flex feed folds into the `cost` basis rather than reporting as a
-- separate column. Measured, that is about 5.3c per closing fill -- $5.93
-- year-to-date, $2.88 in Q4 2025.
--
-- Reverse-engineering that fee stack would mean re-implementing SEC Section 31
-- and FINRA TAF schedules and keeping them current forever. The broker already
-- publishes the answer: `fifoPnlRealized`, per execution. Summed over a period
-- it reproduces the figures in the IBKR app to the cent.
--
-- BOTH, NOT EITHER
--
-- `trades.commission` keeps storing raw `ibCommission`, exactly as IBKR sent
-- it, untouched. That is the provenance record and it stays auditable.
--
-- `trades.broker_realized_pnl` is added beside it: what IBKR says this fill
-- actually realised. The all-in cost of a slice is then derived rather than
-- guessed --
--
--     all_in_cost = our_gross_pnl - broker_realized_pnl
--
-- -- which captures commission and every other charge in one figure, because
-- the broker's number already has them all subtracted.
--
-- WHAT THE DERIVED FEE ACTUALLY CONTAINS
--
-- Stated plainly, because it is a plug: it is everything between our gross and
-- the broker's net. That is overwhelmingly fees, but it also absorbs any small
-- difference between our cost basis and IBKR's -- $9.14 across 166 fills, none
-- individually over $0.42. Net P&L therefore matches the statement exactly by
-- construction, and the fee line is labelled as all-in rather than as
-- commission so it does not claim to be something it is not.
--
-- COVERAGE
--
-- NULL means the broker never told us. A REPAIR- fill has no broker figure by
-- definition, and the Flex window only reaches back 365 days. Those legs fall
-- back to apportioned `ibCommission`, and the coverage is reported rather than
-- assumed -- a journal that silently under-charged fees on the rows it could
-- not verify would be worse than one that says so.

BEGIN;

-- --------------------------------------------------------------------------
-- 1. Staging keeps the broker's figure, exactly as sent.
-- --------------------------------------------------------------------------
-- Refreshed on every sync rather than written once: IBKR re-lots occasionally,
-- and the realised figure for a fill can change after settlement. The raw
-- value belongs here for the same reason `quantity` keeps IBKR's sign here --
-- staging is the unedited copy.
ALTER TABLE ibkr_executions
    ADD COLUMN IF NOT EXISTS fifo_pnl_realized NUMERIC(14, 6);

COMMENT ON COLUMN ibkr_executions.fifo_pnl_realized IS
    'IBKR fifoPnlRealized for this execution, as sent. Net of commission and '
    'every other charge. Zero on an opening fill.';

-- --------------------------------------------------------------------------
-- 2. The ledger carries it alongside the raw commission.
-- --------------------------------------------------------------------------
ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS broker_realized_pnl NUMERIC(12, 4);

COMMENT ON COLUMN trades.broker_realized_pnl IS
    'What IBKR says this fill realised, net of all costs. NULL when unknown '
    '(REPAIR- fills, or fills older than the Flex window). trades.commission '
    'stays the raw ibCommission and is never derived from this.';

-- --------------------------------------------------------------------------
-- 3. Legs record both figures, so the difference stays inspectable.
-- --------------------------------------------------------------------------
-- `commission` becomes the ALL-IN cost -- the figure net P&L is computed from,
-- and the one that makes the dashboard tie to the statement.
--
-- `ib_commission` keeps the apportioned raw commission beside it, so "what did
-- I pay IBKR" and "what did this trade cost me in total" remain two separate,
-- answerable questions rather than one blended number.
ALTER TABLE realized_legs
    ADD COLUMN IF NOT EXISTS ib_commission NUMERIC(14, 4) NOT NULL DEFAULT 0;

-- The broker figure apportioned to this leg, or NULL when the closing fill had
-- none. Stored rather than recomputed so the reconciliation check can compare
-- against it directly without re-deriving the apportionment.
ALTER TABLE realized_legs
    ADD COLUMN IF NOT EXISTS broker_realized_pnl NUMERIC(14, 4);

COMMENT ON COLUMN realized_legs.commission IS
    'ALL-IN cost of this slice: commission plus exchange, clearing and '
    'regulatory charges. Derived as gross_pnl - broker_realized_pnl where the '
    'broker reported one, else the apportioned ibCommission.';
COMMENT ON COLUMN realized_legs.ib_commission IS
    'The apportioned raw ibCommission alone. Provenance, never used for net.';

-- Coverage lookups: "which legs could not be reconciled to the broker?"
CREATE INDEX IF NOT EXISTS ix_realized_legs_unverified
    ON realized_legs (exit_time)
    WHERE broker_realized_pnl IS NULL;

COMMIT;

-- ---------------------------------------------------------------------------
-- BACKFILL
-- ---------------------------------------------------------------------------
-- Existing `trades` rows have no broker figure until a sync supplies one, and
-- the staging insert uses ON CONFLICT DO NOTHING, so a re-sync alone will not
-- fill them in. `ingest_ibkr` therefore refreshes fifo_pnl_realized on every
-- staged row and copies it onto the ledger on each run, which makes an
-- ordinary sync self-healing for history inside the Flex window.
--
-- After the first sync following this migration, rebuild the derived figures:
--
--     POST /api/rematch
--
-- which recomputes every leg's all-in cost from the broker figures now present.

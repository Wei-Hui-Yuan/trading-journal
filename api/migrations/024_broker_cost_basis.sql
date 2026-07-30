-- Migration 024: carry the broker's own acquisition cost, so an open position
--                 reconciles against the statement exactly
--
-- WHY
--
-- Migration 023 fixed realised P&L by taking IBKR's `fifoPnlRealized` instead
-- of reconstructing a fee stack. Open positions have the same problem and no
-- realised figure to lean on: nothing has been sold, so there is no
-- fifoPnlRealized to work backwards from.
--
-- The remaining gap is Singapore GST. IBKR Singapore charges 9% on commission,
-- reports it in the statement as a separate "Sales Tax" line (-5.92 for 2026
-- year-to-date), and folds it into the cost basis of the shares. Our
-- `trades.commission` holds raw `ibCommission` only, so the journal's open rows
-- read fractionally below the statement:
--
--   NFLX  6 shares   ours 94.5299   statement 94.540340167   -0.0104/share
--   VRT   1 share    ours 346.1721  statement 346.187623     -0.0155/share
--
-- Verified as GST rather than guessed: VRT's fill carried 0.3442 of commission
-- across 2 shares, and 9% of that is 0.031 -- exactly the 2-share shortfall.
--
-- WHY NOT JUST MULTIPLY BY 1.09
--
-- Because that is a tax rate hardcoded into a trading journal meant to run for
-- years. Singapore GST moved 7% -> 8% -> 9% across 2022-2024, and the day it
-- moves again every historical basis silently becomes wrong in a way no test
-- would catch. The same objection ruled out reimplementing SEC Section 31 and
-- FINRA TAF schedules in migration 023.
--
-- The broker already publishes the answer. The Flex `cost` attribute on a BUY
-- is the all-in acquisition cost -- notional plus commission plus tax -- so the
-- per-share basis is `cost / quantity` and no rate appears anywhere in our code.
--
-- WHAT IT MEANS ON A SELL
--
-- Nothing usable. On a sale IBKR reports `cost` as the basis RELIEVED, not as
-- proceeds: the AA sell carries -312.8654, precisely the negative of the AA
-- buy's cost. So this column is only consulted for fills that OPEN a long, and
-- anything else falls back to price plus raw commission. A short opened by a
-- sell is therefore still approximate by the tax; this account has had exactly
-- one short, intraday, and being a cent out on a rare case is better than
-- inventing a meaning for an ambiguous field.

BEGIN;

ALTER TABLE ibkr_executions
    ADD COLUMN IF NOT EXISTS broker_cost NUMERIC(18, 8);

COMMENT ON COLUMN ibkr_executions.broker_cost IS
    'IBKR Flex `cost` for this execution, as sent. On a BUY: the all-in '
    'acquisition cost (notional + commission + tax). On a SELL: the cost basis '
    'relieved, which is not an acquisition cost and is not used as one.';

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS broker_cost_basis NUMERIC(18, 8);

COMMENT ON COLUMN trades.broker_cost_basis IS
    'All-in acquisition cost the broker reported for this fill. Consulted only '
    'for fills that open a long; NULL means fall back to price + commission.';

COMMIT;

-- ---------------------------------------------------------------------------
-- BACKFILL
-- ---------------------------------------------------------------------------
-- Same path as migration 023: `ingest_ibkr` refreshes this on every staged row
-- and copies it onto the ledger each run, so an ordinary sync fills in history
-- inside the Flex window. Nothing needs rebuilding afterwards -- open exposure
-- is computed on read, not stored.

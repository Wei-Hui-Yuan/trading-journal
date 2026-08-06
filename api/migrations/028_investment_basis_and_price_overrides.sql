-- Migration 028: manual price override, and an auditable way to correct a
-- wrong quantity or average cost
--
-- WHY PRICE IS A COLUMN AND COST/QUANTITY ARE NOT
--
-- current_price is a fact with one owner: refresh_prices overwrites it
-- wholesale from FMP, nothing else ever reads or writes it, and no other
-- figure is derived FROM it inside a chain that matters if it drifts. A
-- manual_price column sitting next to it, read preferentially, is a clean
-- override with a trivial "reset to auto" (clear the column).
--
-- Quantity and average cost are not facts, they are DERIVED -- _derive_position
-- walks investment_transactions and folds it into a running total, and that
-- same running total is what a SELL relieves cost against to book realized
-- P&L. A raw override column bypasses the ledger entirely: it would fix what
-- the table shows today while leaving every SELL still computing its realized
-- P&L from the OLD, wrong average, and every future re-derivation reverting
-- straight back the moment a new transaction is recorded. Two sources of
-- truth that only sometimes agree is worse than the wrong number was.
--
-- The fix is a transaction, not a column: a correction is itself a ledger
-- entry, folded into the SAME chronological derivation as everything else.
-- Dated "now", it corrects the position from this point forward without
-- rewriting history -- P&L already realized on a past SELL stays exactly as
-- it was booked, and any FUTURE sell correctly uses the corrected average.
-- That is also the whole reason this needs a new transaction_type rather than
-- reusing TRANSFER: TRANSFER's cost effect is `cost += abs(amount)`, which can
-- only ever push cost basis UP. Fixing an average that is too HIGH needs a
-- signed delta, which is a different arithmetic rule, not a different amount.

BEGIN;

ALTER TABLE investment_holdings
    ADD COLUMN IF NOT EXISTS manual_price     NUMERIC(18, 4),
    ADD COLUMN IF NOT EXISTS manual_price_at  TIMESTAMPTZ;

ALTER TABLE investment_transactions
    DROP CONSTRAINT IF EXISTS investment_transactions_type_check,
    ADD CONSTRAINT investment_transactions_type_check
        CHECK (transaction_type IN ('BUY', 'SELL', 'DIVIDEND', 'TRANSFER', 'ADJUSTMENT'));

-- ADJUSTMENT repurposes quantity and total_amount as SIGNED DELTAS rather
-- than the magnitudes every other type carries -- a share count that was
-- overcounted corrects with a negative quantity, which BUY/SELL/TRANSFER's
-- "> 0" rule exists specifically to forbid for an ordinary trade. price stays
-- NULL: a correction was not bought at a price, so no CHECK is added for it,
-- and the application never sets one.
ALTER TABLE investment_transactions
    DROP CONSTRAINT IF EXISTS investment_transactions_quantity_check,
    ADD CONSTRAINT investment_transactions_quantity_check
        CHECK (
            (transaction_type IN ('BUY', 'SELL', 'TRANSFER')
             AND quantity IS NOT NULL AND quantity > 0)
            OR
            (transaction_type = 'DIVIDEND' AND quantity IS NULL)
            OR
            (transaction_type = 'ADJUSTMENT' AND quantity IS NOT NULL)
        );

COMMENT ON COLUMN investment_holdings.manual_price IS
    'Overrides current_price for market value / valuation when set. '
    'current_price keeps being overwritten wholesale by refresh_prices '
    'underneath it, so clearing this reverts to the live quote with nothing '
    'to re-fetch.';

COMMENT ON CONSTRAINT investment_transactions_type_check ON investment_transactions IS
    'ADJUSTMENT is deliberately NOT offered by the general create-transaction '
    'API (see TX_TYPES vs TX_ADJUSTMENT in main.py) -- it is written only by '
    'the basis-correction endpoint, which computes the signed delta itself.';

COMMIT;

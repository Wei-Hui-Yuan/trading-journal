-- Migration 020: charge commissions against realised P&L
--
-- The matching engine computed (exit - entry) x quantity and stored it as
-- `realized_pnl`. That is GROSS P&L. Commission was parsed from the Flex
-- statement and staged into ibkr_executions.commission on every one of the 328
-- fills, and then read by nothing -- so net P&L, profit factor, expectancy,
-- the equity curve, drawdown and win rate were all computed gross.
--
-- Win rate is the one that misleads rather than merely overstates. A trade that
-- made $0.40 gross and paid $0.36 in commission counted as a win at full size.
-- On this account a 0.1-share FUTU fill paid 1.00% of notional in commission,
-- and $102.56 of commission sits against $190.13 of gross losses.
--
-- After this, `realized_pnl` means NET -- what actually reached the account --
-- because every downstream consumer already reads that column and should not
-- have to know about this change. `gross_pnl` and `commission` are stored
-- beside it so the difference is inspectable rather than implied, and so
-- gross_pnl - commission = realized_pnl can be checked at any time.
--
-- SIGN. IBKR reports ibCommission as a NEGATIVE number for a charge, but not
-- always: 10 of the 328 staged fills carry a POSITIVE value, which is a
-- rebate (tiered pricing passes exchange liquidity rebates through). So the
-- normalisation is NEGATION, not ABS. Taking the absolute value would turn
-- $0.96 of credits into $0.96 of charges -- a $1.92 error in the wrong
-- direction, on the very figure this migration exists to make honest.
-- `trades.commission` is therefore a COST: positive is money paid, and
-- negative is a rebate received.
--
-- Staging keeps IBKR's raw sign, exactly as ibkr_executions.quantity already
-- keeps IBKR's raw sign. Normalisation happens once, on the way into `trades`.

BEGIN;

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS commission NUMERIC(14, 6) NOT NULL DEFAULT 0;

-- Deliberately no CHECK (commission >= 0): a rebate is a real, negative cost.
COMMENT ON COLUMN trades.commission IS
    'Commission as a COST: positive is paid, negative is a rebate. IBKR''s '
    'ibCommission uses the opposite sign and is negated on promotion.';

ALTER TABLE positions
    -- Nullable: a row predating this migration has no separately recorded
    -- gross figure until matching re-runs, and a wrong 0 would read as a
    -- scratch trade. Backfilled below, so in practice it is always set.
    ADD COLUMN IF NOT EXISTS gross_pnl  NUMERIC(12, 4),
    ADD COLUMN IF NOT EXISTS commission NUMERIC(12, 4) NOT NULL DEFAULT 0;

COMMENT ON COLUMN positions.realized_pnl IS
    'NET realised P&L: gross_pnl - commission. What reached the account.';
COMMENT ON COLUMN positions.gross_pnl IS
    'Realised P&L before commission: (exit - entry) x quantity.';
COMMENT ON COLUMN positions.commission IS
    'Commission apportioned to this round trip, by the fraction of each fill '
    'it consumed. A cost: positive is paid, negative is a net rebate.';

UPDATE trades t
   SET commission = -e.commission
  FROM ibkr_executions e
 WHERE t.ibkr_exec_id = 'IBKR-' || e.transaction_id
   AND e.commission IS NOT NULL;

-- Every existing position was matched without commissions, so what it stores
-- today IS its gross figure and it has been charged nothing. Recording that
-- makes gross_pnl - commission = realized_pnl true the moment this commits,
-- rather than only after the re-match that follows it. The re-match then
-- rewrites all three together.
UPDATE positions SET gross_pnl = realized_pnl WHERE gross_pnl IS NULL;

COMMIT;

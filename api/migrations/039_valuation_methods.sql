-- Migration 039: three base flows instead of one, and a choice between them
--
-- WHY THIS EXISTS
--
-- The DCF has always run on exactly one number -- free cash flow -- because
-- that is what the reference workbook feeds it. `metric` records which,
-- and it reads 'free_cash_flow' on all 24 auto rows today. The engine
-- itself is indifferent: valuation.py's docstring says base_flow is "free
-- cash flow, operating cash flow, or net income depending on the method
-- chosen -- the model does not care which, only that it is the flow being
-- grown". That indifference was written down and then never used.
--
-- The reference tool publishes three bars off exactly that indifference:
--
--     DFCF-20   free cash flow      (what we already compute)
--     DCF-20    operating cash flow
--     DNI-20    net income
--
-- Same twenty years, same discount rate, same growth schedule, same debt
-- and cash bridge -- one different input. The gap between them is the
-- output: a company whose DCF-20 towers over its DFCF-20 is spending its
-- operating cash on capex, and a DNI-20 far above both is earning on paper
-- what it is not collecting.
--
-- NEITHER NEW FLOW COSTS AN API CALL
--
-- operating_cash_flow has been fetched on every refresh since the beginning
-- and thrown away at the point of storage -- market_data.py populates
-- Fundamentals.operating_cash_flow_m from both providers and main.py's
-- values dict never wrote it down. Net income is the top line of the cash
-- flow statement both providers already return (FMP's `netIncome`,
-- Finnhub's `NetIncomeLoss`), so it too comes out of a response already on
-- the wire. This migration is what lets them be kept.
--
-- WHY THEY ARE COLUMNS RATHER THAN A REUSE OF base_flow
--
-- base_flow is what the model was FED. It has to keep meaning that, because
-- the override system edits it and a trader who hand-keys ASML's free cash
-- flow must not have that silently reinterpreted as net income when they
-- switch methods. Three columns means three flows can coexist -- fetched or
-- hand-keyed, independently -- and switching method re-reads rather than
-- rewrites. All three go through the same merge in _merge_inputs, so an
-- override supplies any of them the same way it supplies the rest.
--
-- WHY THE CHOICE LIVES ON THE HOLDING
--
-- valuation_method is not an input to the model; it selects between three
-- results the model has already produced. Every one of them is returned on
-- every request regardless, so the modal can show all three side by side --
-- this column only decides which drives premium_pct and the portfolio
-- table's column, i.e. which of the three the app treats as this holding's
-- valuation. Defaulted rather than nullable so no row has to be backfilled
-- and today's behaviour is exactly preserved: every existing holding keeps
-- valuing on free cash flow until someone deliberately changes it.

BEGIN;

ALTER TABLE investment_valuation_inputs
    ADD COLUMN IF NOT EXISTS operating_cash_flow NUMERIC(20, 4),
    ADD COLUMN IF NOT EXISTS net_income NUMERIC(20, 4);

ALTER TABLE investment_holdings
    ADD COLUMN IF NOT EXISTS valuation_method VARCHAR(24)
        NOT NULL DEFAULT 'free_cash_flow';

COMMENT ON COLUMN investment_valuation_inputs.operating_cash_flow IS
    'Cash from operations, in the same millions-of-statement-currency unit '
    'as base_flow. Feeds the DCF-20 model. Latest filed year only -- '
    'unsmoothed, unlike base_flow, because the multi-year averaging in '
    '_smoothed_free_cash_flow exists to absorb a lumpy CAPEX year and this '
    'figure is measured before capex.';

COMMENT ON COLUMN investment_valuation_inputs.net_income IS
    'Net income for the latest filed year, same unit as base_flow. Feeds '
    'the DNI-20 model. May legitimately be negative -- an unprofitable '
    'year is a real reading, not a missing one -- and the engine declines '
    'to value a non-positive flow rather than returning a negative '
    'intrinsic value.';

COMMENT ON COLUMN investment_holdings.valuation_method IS
    'Which of the three base flows this holding is valued on: '
    '''free_cash_flow'' (DFCF-20, the default and what every row valued on '
    'before this column existed), ''operating_cash_flow'' (DCF-20) or '
    '''net_income'' (DNI-20). Selects between results rather than feeding '
    'them: all three are computed and returned on every request, and this '
    'decides which one premium_pct and the portfolio table report.';

COMMIT;

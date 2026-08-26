-- Migration 037: the DCF's OWN currency conflation (issue #5, part 2)
--
-- WHY THIS EXISTS
--
-- value_scenario (api/services/valuation.py) computes `per_share` from
-- base_flow/shares/debt/cash, which the module's own docstring says "must
-- all be in the financial statement's currency" -- then multiplies it by
-- the HOLDING's exchange_rate to land in the listing currency. That is only
-- correct when the statement currency IS USD, because exchange_rate means
-- "1 USD in the listed currency" everywhere else in this app (migration
-- 026's own comment). For a company that files in a different currency
-- than it lists in -- ASML (EUR, 20-F, listed as a USD ADR), Novo Nordisk
-- (DKK) -- the current math skips a conversion hop entirely and silently
-- treats a EUR or DKK per-share figure as if it were already USD.
--
-- Currently latent for the same reason issue #5's first bug was: every
-- company in this book today files in USD, so exchange_rate's two different
-- meanings (USD-to-listed, and the statement-to-listed conversion valuation.py
-- actually performs) happen to coincide.
--
-- THE FIX
--
-- A second rate: statement_exchange_rate, "1 USD in the statement's filing
-- currency" -- same convention as the existing exchange_rate, one hop
-- earlier. value_scenario becomes a proper two-hop conversion:
-- statement -> USD (divide by statement_exchange_rate) -> listed (multiply
-- by exchange_rate). When both stages are USD, statement_exchange_rate is
-- 1 and the formula is unchanged from today.
--
-- WHY IT CANNOT BE AUTO-FETCHED
--
-- There is no FX-rate provider anywhere in this app (see market_data.py) --
-- exchange_rate itself is manually maintained, per the decision on issue
-- #5. So this column auto-populates to 1.0 on refresh ONLY when the
-- fetched statement currency is USD (the common case: GOOGL, MSFT, META,
-- NVDA, AMZN, UNH all file in USD). For anything else it is left NULL, and
-- _value_holding treats a NULL statement_exchange_rate the same as a
-- missing base_flow or shares_outstanding: valuation is unavailable until
-- the trader supplies the rate by hand, rather than silently assuming 1.0
-- the way the code did before this migration.
--
-- statement_currency is stored purely for display -- so the override modal
-- can say WHY a rate is being asked for ("ASML files in EUR") -- and is not
-- itself part of the DCF math or the override system; only the numeric
-- rate is.

BEGIN;

ALTER TABLE investment_valuation_inputs
    ADD COLUMN IF NOT EXISTS statement_currency VARCHAR(3),
    ADD COLUMN IF NOT EXISTS statement_exchange_rate NUMERIC(18, 8);

COMMENT ON COLUMN investment_valuation_inputs.statement_currency IS
    'The currency the company''s financial statements are filed in (e.g. '
    '"EUR" for ASML), as fetched. Display/context only -- not read by the '
    'DCF math and not part of the override system; see '
    'statement_exchange_rate for the number that actually converts it.';

COMMENT ON COLUMN investment_valuation_inputs.statement_exchange_rate IS
    '1 USD in statement_currency, same convention as '
    'investment_holdings.exchange_rate one hop earlier. Auto-populated to '
    '1.0 only when statement_currency is USD; NULL otherwise, since there '
    'is no FX-rate provider to fetch it from -- the trader supplies it by '
    'hand via the valuation override, and the DCF declines to value the '
    'holding until they do (see _value_holding), rather than silently '
    'treating the statement currency as USD.';

COMMIT;

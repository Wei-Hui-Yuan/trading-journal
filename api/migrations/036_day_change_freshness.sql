-- Migration 036: day_change_updated_at -- the day's move can go stale
-- independently of the price it sits next to
--
-- WHY THIS EXISTS
--
-- Migration 029 reasoned that day_change_pct always arrives in the same
-- response as current_price, so price_updated_at could double as both
-- figures' freshness and a second timestamp "could only ever say the same
-- thing or be wrong." That assumption does not hold in the code three lines
-- below it: refresh_prices (api/main.py) writes price_updated_at on every
-- successful quote, but only overwrites day_change_pct when the provider's
-- response actually included changePercentage that time -- a real,
-- confirmed gap, not a hypothetical one (see fetch_quote's own docstring in
-- api/services/market_data.py: "a missing price is fatal ... a missing
-- change is not, and stays None"; pinned by
-- test_a_missing_day_change_leaves_the_last_one_standing in
-- api/tests/test_price_refresh.py). When that happens, price_updated_at
-- advances to the new refresh while day_change_pct is quietly left at
-- whatever it was last time -- a stale number sitting under a fresh
-- timestamp, with nothing on record to say the two had drifted apart.
--
-- This column is written only when day_change_pct is, so a reader can tell
-- "this day figure came from the same refresh as the current price" (the
-- two timestamps match exactly -- refresh_prices stamps both from the same
-- `now`) from "this day figure is left over from an earlier one" (they
-- don't) -- see isDayChangeStale in AllocationPanel.tsx.

BEGIN;

ALTER TABLE investment_holdings
    ADD COLUMN IF NOT EXISTS day_change_updated_at TIMESTAMPTZ;

-- Best-effort backfill: there is no historical record of when an existing
-- day_change_pct was actually captured, so an existing value is assumed
-- fresh as of its holding's price_updated_at. That may be generous for a
-- handful of rows that had already drifted before this migration ran, but
-- every refresh from here on is tracked exactly, so the gap self-corrects.
UPDATE investment_holdings
SET day_change_updated_at = price_updated_at
WHERE day_change_pct IS NOT NULL;

COMMENT ON COLUMN investment_holdings.day_change_pct IS
    'Percent change on the day as reported by FMP, as of '
    'day_change_updated_at -- NOT necessarily the same refresh as '
    'price_updated_at/current_price; compare the two timestamps before '
    'presenting this as today''s move. NULL means no refresh has ever '
    'fetched it, which is not the same as 0.';

COMMENT ON COLUMN investment_holdings.day_change_updated_at IS
    'When day_change_pct was last actually written by refresh_prices. '
    'Equal to price_updated_at when both came from the same refresh; older '
    'than it when a later refresh fetched a new price but the provider '
    'omitted the day change that time, leaving the prior figure in place.';

COMMIT;

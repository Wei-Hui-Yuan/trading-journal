-- Migration 029: the day's price move, so the treemap can be colored by
-- performance instead of by sector
--
-- WHY THIS COSTS NOTHING TO COLLECT
--
-- refresh_prices already calls FMP's `profile` endpoint once per holding, and
-- that response already carries `changePercentage` alongside `price` --
-- fetch_quote simply read the one field and discarded the other. So this is
-- not a new provider call, a new provider, or a new rate-limit surface: it is
-- a field that was arriving and being thrown away.
--
-- WHY A PERCENT AND NOT THE ABSOLUTE MOVE
--
-- The only consumer is a color scale, which is comparative by nature -- a
-- $4 move means nothing next to another holding's $4 move unless both are
-- divided by their price first. Storing the absolute change too would mean
-- two columns that must agree, to serve a reader that only ever wants one.
--
-- WHY IT IS ALLOWED TO GO STALE
--
-- This is a snapshot taken when refresh_prices last ran, not a live feed, and
-- it deliberately shares price_updated_at rather than carrying its own
-- timestamp: both figures come from the same response in the same call, so a
-- second timestamp could only ever say the same thing or be wrong. The UI
-- reads that column to say how old the number is, rather than implying the
-- tape is live.

BEGIN;

ALTER TABLE investment_holdings
    ADD COLUMN IF NOT EXISTS day_change_pct NUMERIC(10, 4);

COMMENT ON COLUMN investment_holdings.day_change_pct IS
    'Percent change on the day as reported by FMP when refresh_prices last '
    'ran -- e.g. -1.0900 for -1.09%. Stale between refreshes by design; '
    'price_updated_at is the freshness of both this and current_price. NULL '
    'means no refresh has fetched it yet, which is not the same as 0.';

COMMIT;

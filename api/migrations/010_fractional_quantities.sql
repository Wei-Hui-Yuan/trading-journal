-- Migration 010: store share counts exactly, including fractions.
--
-- Every quantity column was INTEGER, on the assumption that fills come in
-- whole shares. This account trades fractional almost exclusively: of 55
-- equity fills in a year of history, 38 were fractional and 27 of those were
-- smaller than half a share.
--
-- The consequences were silent and severe. Rounding half-up meant a 0.25 AMZN
-- or 0.1 MELI fill became 0 and was discarded as "zero quantity" -- a real
-- position worth hundreds of dollars vanishing with only a log line. Anything
-- from 0.5 up was inflated to 1, so a 0.65 PLTR buy was recorded 54% larger
-- than it was, and every derived figure -- P&L, R-multiple, expectancy --
-- inherited that error.
--
-- NUMERIC(18, 8) is chosen over DOUBLE PRECISION deliberately: share counts
-- feed directly into money arithmetic, and binary floating point cannot
-- represent decimal fractions exactly. 8 decimal places also covers the
-- sub-cent currency-conversion rows IBKR reports.
--
-- Widening is lossless -- existing whole-share values are unaffected -- but it
-- does not recover data already dropped. Re-sync the affected date range to
-- backfill fills that earlier runs discarded.

ALTER TABLE trades
    ALTER COLUMN quantity TYPE NUMERIC(18, 8);

ALTER TABLE ibkr_executions
    ALTER COLUMN quantity TYPE NUMERIC(18, 8);

ALTER TABLE position_fills
    ALTER COLUMN quantity TYPE NUMERIC(18, 8);

-- Already NUMERIC, but only (12, 4). Widened so a position can never be
-- rounded to a different size than the fills that composed it.
ALTER TABLE positions
    ALTER COLUMN quantity TYPE NUMERIC(18, 8);

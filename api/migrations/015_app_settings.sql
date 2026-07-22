-- Migration 015: account size and default risk, stored server-side.
--
-- The position-size calculator needs two numbers the ledger has never held:
-- how big the account is, and what fraction of it a trade is allowed to risk.
-- Keeping them in the browser would tie them to one device and put them out of
-- reach of any query, so they live here instead.
--
-- WHY A SINGLETON, AND WHY NO HISTORY
--
-- This is a single-trader journal, so `app_settings` is one row, pinned by
-- `CHECK (id = 1)`. That constraint is what makes GET/PUT total: there is
-- always exactly one row to read and update, and no endpoint has to decide
-- which settings row it meant.
--
-- Deliberately NOT a time series. Account size drifts as the account grows, and
-- the temptation is to version it here so an old trade can be read against the
-- balance of its day. But that history already exists, in the right place:
-- `trades.risk_amount` records the dollars actually put at risk on each fill,
-- captured at entry. A settings table that also tried to hold history would be
-- a second, competing answer to the same question -- and the two would drift
-- the first time a trade was logged with a hand-edited risk figure.
--
-- So: this table holds the CURRENT default, which is a form convenience. The
-- ledger holds what each trade actually risked, which is the analytical record.

CREATE TABLE IF NOT EXISTS app_settings (
    -- Not a UUID, unlike every other table here. The point of this column is to
    -- be unforgeable rather than unique -- there is nothing to reference it.
    id            INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    -- Nullable: an account size nobody has set yet is unknown, not zero. The
    -- calculator hides its share count rather than confidently suggesting 0.
    account_size  NUMERIC(14, 2),
    -- Percent, not fraction: 1.00 means 1%, matching trades.risk_percent so the
    -- default and the recorded value are the same unit and directly comparable.
    risk_percent  NUMERIC(5, 2) NOT NULL DEFAULT 1.00,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seeded with the values this journal has used since September 2025, so the
-- calculator is usable on first open rather than showing an empty form.
INSERT INTO app_settings (id, account_size, risk_percent)
VALUES (1, 2500.00, 1.00)
ON CONFLICT (id) DO NOTHING;

-- --------------------------------------------------------------------------
-- Widen trades.risk_percent so real positions fit.
-- --------------------------------------------------------------------------
-- It was NUMERIC(4,2), which caps at 99.99. That is fine for the intended 1-3%
-- rule but not for what actually gets recorded: the calculator now derives
-- risk_percent from the quantity ACTUALLY entered, and a margined position
-- whose stop distance times its size exceeds the account balance computes
-- above 100%. On the old type that is not a clamp, it is an integrity error at
-- insert -- a 500 on a trade the user is entitled to log.
--
-- Widening beats clamping. A clamp would silently record 99.99 for a position
-- that risked 140%, which is precisely the outlier a risk journal exists to
-- show. NUMERIC(6,2) is a superset of the old type, so no data changes.
ALTER TABLE trades ALTER COLUMN risk_percent TYPE NUMERIC(6, 2);

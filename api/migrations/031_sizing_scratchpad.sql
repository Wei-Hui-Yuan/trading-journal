-- Migration 031: a scratchpad for sizing a trade before there is time to plan it
--
-- THE PROBLEM
--
-- Writing a real plan (`planned_trades`) means opening the Plan modal and
-- thinking about a thesis, a strategy, conviction -- worthwhile when there is
-- time for it, and exactly the thing there is no time for when a price is
-- moving and the only question that matters right now is "how many shares".
--
-- WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
--
-- A holding pen for the four numbers that answer that question: entry, stop,
-- take profit, quantity. Nothing here is read by the matching engine, by
-- analytics, or by anything outside the sizing-scratchpad endpoints in
-- main.py. It carries no thesis, no strategy, no conviction, and cannot
-- attach to a fill -- those are exactly the things `planned_trades` exists
-- for, and giving this table any of them would make it a second, worse copy
-- of a plan rather than the fast path to writing a real one.
--
-- The bridge between the two is one endpoint, POST .../promote, and it is
-- one-way: promoting turns a scratch note into a real plan and deletes the
-- note in the same transaction. There is no path back.
--
-- WHY NO COMPUTED COLUMNS
--
-- Risk, R and target profit are pure functions of entry/stop/take_profit/
-- quantity, and src/lib/positionSizing.ts already computes exactly this for
-- the Plan modal. Storing a second copy of the same arithmetic here would
-- only give it a second place to drift from the first.
--
-- WHY THIS EXPIRES ITSELF WITHOUT A CRON JOB
--
-- Every row this table will ever hold is a handful of small numbers -- not a
-- storage concern at any scale this app will reach. The three-day clearing is
-- about SIGNAL, not space: a note about a trade from four days ago is noise,
-- not history, and the honest place to enforce that is the read path itself.
-- GET /api/sizing-scratchpad deletes anything older than three days before it
-- selects, in the same request -- so "the log clears itself" is true the
-- moment the tab is next opened, with no Northflank Cron Job to configure and
-- nothing to forget to wire up.

BEGIN;

CREATE TABLE IF NOT EXISTS sizing_scratchpad (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticker       VARCHAR(10)    NOT NULL,
    direction    VARCHAR(5)     NOT NULL,
    -- All four nullable, matching planned_trades' own philosophy: a note is
    -- worth keeping the moment it has a ticker, not only once every field is
    -- filled in. Widths match planned_trades exactly, so a promoted entry
    -- moves into its NUMERIC columns losslessly.
    entry        NUMERIC(10, 4),
    stop_loss    NUMERIC(10, 4),
    take_profit  NUMERIC(10, 4),
    quantity     NUMERIC(18, 8),
    created_at   TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

ALTER TABLE sizing_scratchpad
    DROP CONSTRAINT IF EXISTS sizing_scratchpad_direction_check;

ALTER TABLE sizing_scratchpad
    ADD CONSTRAINT sizing_scratchpad_direction_check
    CHECK (direction IN ('BUY', 'SELL'));

-- The read path's own age filter does the real work; this only keeps that
-- DELETE from ever needing a sequential scan as the table grows.
CREATE INDEX IF NOT EXISTS ix_sizing_scratchpad_created_at
    ON sizing_scratchpad (created_at);

COMMIT;

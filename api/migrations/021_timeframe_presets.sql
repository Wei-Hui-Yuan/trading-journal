-- Migration 021: saved timeframe presets, so a custom window follows the trader
--
-- The dashboard filters every figure it reports -- KPI strip, heatmap and
-- equity curve -- to a window. Three built-ins (YTD, 1Y, ALL) are resolved in
-- code and never stored: they are definitions, not data, and a row saying
-- "1Y means 365 days" would be free to disagree with the code that computes it.
--
-- Custom windows ARE data. "2025 Full Year" is a decision the trader made, and
-- keeping it in localStorage would tie it to one browser -- open the journal on
-- a phone and the window you always look at is simply gone.
--
-- WHY A TABLE AND NOT app_settings.custom_timeframes JSONB
--
-- A JSONB array on the singleton settings row is the shorter path and was the
-- obvious candidate, since `app_settings` already exists and this is a
-- single-user journal. It is rejected for reasons that are specific rather
-- than stylistic:
--
--   * Every edit becomes read-modify-write of the whole array. Two tabs open on
--     the dashboard, each adding a preset, and the second write silently drops
--     the first. A row per preset makes each mutation independent.
--   * `start_date <= end_date` is the invariant that matters here, and a CHECK
--     can enforce it. Inside a JSON document nothing can -- a backwards window
--     would be storable, and would return an empty dashboard with no
--     explanation.
--   * DATE is a real type. In JSON these are strings, and "2025-1-1",
--     "01/01/2025" and "2025-01-01" are all equally storable and only one of
--     them parses.
--   * The API shape the UI needs is PUT/DELETE by id. That is a table.
--
-- This is the opposite call from `trades.broker_original`, which is JSONB on
-- purpose -- but that column is written once, never queried, and exists only to
-- be displayed. These rows are edited, deleted and looked up by id.
--
-- NAMED `timeframe_presets`, not `user_timeframes`: there is no user table and
-- no user_id column anywhere in this schema, and a name promising per-user
-- scoping that nothing enforces is worse than one that does not.

BEGIN;

CREATE TABLE IF NOT EXISTS timeframe_presets (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- What the pill says. Free text: this is the trader's own label for a
    -- window, and "Post-tariff chop" is as legitimate as "2025 Full Year".
    name        TEXT NOT NULL,

    -- DATE, not TIMESTAMPTZ. A preset is a calendar range in market time --
    -- "all of 2025" -- not a pair of instants. The dashboard buckets closes by
    -- their America/New_York calendar date for exactly the same reason, so the
    -- window boundary and the curve's own buckets are expressed in one unit
    -- and cannot land a trade on the wrong side of the edge.
    start_date  DATE NOT NULL,
    end_date    DATE NOT NULL,

    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- A backwards window is not a filter, it is an empty dashboard with no
    -- explanation. Rejected here so it cannot be written by any path,
    -- including a hand-run UPDATE.
    CONSTRAINT timeframe_presets_range_check
        CHECK (start_date <= end_date),

    -- '   ' is not a name. The API trims and rejects blanks too; this is the
    -- backstop for anything that does not go through it.
    CONSTRAINT timeframe_presets_name_not_blank
        CHECK (btrim(name) <> '')
);

-- One pill per label. Case- and whitespace-insensitive, because "2025" and
-- "2025 " render identically and two pills that look the same are a bug
-- report, not a feature. Functional index rather than a UNIQUE column, so the
-- trader's own capitalisation is preserved on display.
CREATE UNIQUE INDEX IF NOT EXISTS uq_timeframe_presets_name
    ON timeframe_presets (lower(btrim(name)));

-- The toolbar reads every preset, newest last, on each dashboard load.
CREATE INDEX IF NOT EXISTS ix_timeframe_presets_created_at
    ON timeframe_presets (created_at);

COMMENT ON TABLE timeframe_presets IS
    'Saved custom dashboard windows. The built-in YTD/1Y/ALL presets are '
    'resolved in code and deliberately have no rows here.';

COMMIT;

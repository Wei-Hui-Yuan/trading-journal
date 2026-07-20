-- Migration 003: strategy playbook fields
--
-- Backs the Strategies Management tab: each strategy grows from a bare
-- name/description into a structured playbook (overarching method, entry
-- rules, exit rules).
--
-- All three columns are nullable with a '' DEFAULT, so ADD COLUMN backfills
-- existing rows rather than failing on a NOT NULL constraint. The two
-- strategies already in this database keep working untouched.

ALTER TABLE strategies
    ADD COLUMN IF NOT EXISTS method         TEXT DEFAULT '',
    ADD COLUMN IF NOT EXISTS entry_criteria TEXT DEFAULT '',
    ADD COLUMN IF NOT EXISTS exit_criteria  TEXT DEFAULT '';

-- Backfill any pre-existing NULLs (a column added in an earlier partial run
-- without a DEFAULT would leave NULLs behind).
UPDATE strategies SET method         = '' WHERE method         IS NULL;
UPDATE strategies SET entry_criteria = '' WHERE entry_criteria IS NULL;
UPDATE strategies SET exit_criteria  = '' WHERE exit_criteria  IS NULL;

-- Migration 000: recreate the two tables that predate every migration
--
-- `trades` and `strategies` were created by hand in the Supabase SQL Editor
-- before this migrations/ directory existed. Migration 002 says as much for
-- strategies ("this table already exists from the original schema"), and
-- migration 004 says it for four of trades's columns ("This migration
-- therefore only GUARANTEES the canonical columns exist"). No migration ever
-- issues a CREATE TABLE for `trades` at all -- every one of the 30 that follow
-- assumes it is already there.
--
-- That was invisible for as long as every environment descended from the same
-- hand-created database. It stopped being invisible the moment a genuinely
-- fresh Postgres -- a CI runner with no history -- tried to run these
-- migrations in order and migration 001 failed on its first statement:
-- `relation "trades" does not exist`. There was no environment before this one
-- that had ever started from nothing.
--
-- WHAT THIS RECREATES, AND HOW IT WAS DETERMINED
--
-- Not a copy of the CURRENT schema -- that would skip the real ALTER, DROP and
-- WIDEN statements that migrations 001-030 actually perform, which is exactly
-- the coverage a fresh database is for. This reconstructs the schema as it
-- stood the moment before migration 001 first ran, so every later migration
-- does the same work here that it once did on the original database.
--
-- Determined by reading every migration that touches `trades` or `strategies`
-- and separating two kinds of statement:
--
--   * `ADD COLUMN IF NOT EXISTS` -- the column is NOT required here. If it is
--     missing, the migration that guards it creates it at that point instead,
--     and the end state is identical either way. These are intentionally
--     OMITTED below and left to run naturally.
--
--   * Anything else that assumes a column already exists -- `ALTER COLUMN
--     ... TYPE`, a bare `UPDATE`, a comment stating the column is pre-existing
--     -- names a column with no guard anywhere in the other 30 files. Those,
--     and only those, are recreated here, in their PRE-migration shape:
--
--       trades.quantity        was INTEGER (migration 010 widens it)
--       trades.risk_percent    was NUMERIC(4, 2) (migration 015 widens it)
--       strategies.name        was VARCHAR(50) (migration 002 widens it)
--
-- `strategies.instruments` is a third kind: no migration ever touches it at
-- all, guarded or otherwise, so nothing would create it even on a fresh
-- database that ran every file. It exists only because main.py's ORM model
-- has always declared it. Missing entirely from the migration history, and
-- added here for exactly that reason.
--
-- SAFE ON A DATABASE THAT ALREADY HAS THESE TABLES. Every statement is
-- guarded (`IF NOT EXISTS`), so running this against production -- where both
-- tables already exist with every later migration already applied -- does
-- nothing. It has to be safe there: this file did not exist when production
-- was baselined, so it is genuinely new and pending the next real run.

BEGIN;

-- strategies before trades: trades.strategy_id is a foreign key into it, and
-- referencing a table that is not there yet fails the same way `trades` did.
CREATE TABLE IF NOT EXISTS strategies (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(50) NOT NULL UNIQUE,
    description TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Never added by any migration, guarded or otherwise -- only ever assumed, by
-- main.py's ORM model, to be there.
ALTER TABLE strategies
    ADD COLUMN IF NOT EXISTS instruments TEXT[] DEFAULT '{}';

CREATE TABLE IF NOT EXISTS trades (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Dedup key for broker syncs; nullable because a hand-logged trade has
    -- none until migration 018 renames it into the MANUAL-/REPAIR- scheme and
    -- adds the CHECK constraint that then requires it.
    ibkr_exec_id   VARCHAR(100) UNIQUE,
    ticker         VARCHAR(10)    NOT NULL,
    direction      VARCHAR(5)     NOT NULL,
    style          VARCHAR(15)    NOT NULL,
    entry_date     TIMESTAMPTZ    NOT NULL,
    exit_date      TIMESTAMPTZ,
    actual_entry   NUMERIC(10, 4) NOT NULL,
    -- Original type. Migration 010 widens this to NUMERIC(18, 8) for
    -- fractional shares -- that ALTER COLUMN TYPE needs the column to already
    -- exist, which is why it is not merely an ADD COLUMN guard.
    quantity       INTEGER        NOT NULL,
    -- Original type and default. Migration 015 widens this to NUMERIC(6, 2)
    -- for the same reason: ALTER COLUMN TYPE, not ADD COLUMN.
    risk_percent   NUMERIC(4, 2) DEFAULT 1.00,
    strategy_id    UUID REFERENCES strategies(id) ON DELETE SET NULL,
    grade          CHAR(1),
    market_regime  VARCHAR(20),
    source_tag     VARCHAR(10) DEFAULT 'Own',
    screenshot_url TEXT,
    hard_sl_set    BOOLEAN DEFAULT TRUE,
    waited_retest  BOOLEAN DEFAULT TRUE,
    followed_plan  BOOLEAN DEFAULT TRUE,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

COMMIT;

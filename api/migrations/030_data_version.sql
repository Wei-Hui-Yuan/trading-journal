-- Migration 030: one counter that answers "has anything changed?" in a single read
--
-- THE PROBLEM
--
-- Every analytics request rebuilds its answer from a full table scan. The
-- dashboard reads every closed position and every realised leg, the journal
-- reads every execution and every fill, and both do it again on the next page
-- load whether or not a single row moved in between. On a page that fires six
-- requests at once, that is six full passes over the ledger to produce a
-- payload byte-for-byte identical to the one already in the browser.
--
-- HTTP already has the answer to this -- an ETag, a 304, and no body. What it
-- needs is a validator: something that changes when the data changes and is
-- cheap enough to check that computing it does not cost what it saves. The
-- 304 is only worth having if producing it skips the scan.
--
-- WHY NOT max(updated_at)
--
-- The obvious validator is MAX(updated_at) per table. It does not work here,
-- for two independent reasons:
--
--   * These tables have created_at and no updated_at. A review being written,
--     a grade being changed, a rematch rewriting realized_pnl -- none of those
--     touch created_at, so a cache keyed on it would serve a stale dashboard
--     after exactly the edits a trading journal exists to record.
--
--   * COUNT(*) would be needed alongside it to notice deletions, and in
--     Postgres COUNT(*) is a scan. The validator would cost the thing it was
--     meant to avoid.
--
-- Adding updated_at to eight tables plus triggers to maintain it, plus an
-- index on each to keep MAX() cheap, is a lot of moving parts and a write cost
-- on every table, to end up with eight numbers to compare instead of one.
--
-- WHAT THIS DOES INSTEAD
--
-- A single row holding a counter, incremented by a statement-level trigger on
-- every table the cached endpoints read. Checking it is one primary-key lookup
-- returning one bigint -- roughly the cheapest query Postgres can answer. It
-- notices inserts, updates AND deletes, because the trigger fires on all three
-- and does not care how many rows were involved.
--
-- Deliberately ONE counter rather than one per endpoint. A rename of a strategy
-- invalidates the equity curve, which it did not really need to; that is the
-- price, and it is the right one to pay. Per-endpoint scopes mean every future
-- endpoint has to correctly declare which tables it depends on, and the failure
-- mode of getting that wrong is a stale figure that looks plausible -- the
-- worst kind of bug for a system whose whole job is being trusted with numbers.
-- Over-invalidating costs a recomputation nobody notices.
--
-- FOR EACH STATEMENT, not FOR EACH ROW. The ingest path inserts hundreds of
-- executions in one statement and the matching engine rewrites whole tickers;
-- a row-level trigger would bump the counter once per row, taking the same row
-- lock hundreds of times to reach a value nobody reads the intermediate states
-- of. Statement-level fires once per statement regardless of row count.
--
-- The counter is a version, not a timestamp. Two changes inside the same
-- millisecond are indistinguishable by NOW(); they are not by a counter, and
-- the whole point is to never serve a cached payload that a write has passed.

BEGIN;

CREATE TABLE IF NOT EXISTS data_version (
    scope       TEXT        PRIMARY KEY,
    version     BIGINT      NOT NULL DEFAULT 0,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE data_version IS
    'One row per cache scope, holding a counter bumped by triggers on every '
    'table the cached read endpoints depend on. Read by the ETag helpers in '
    'main.py to answer a conditional request without rebuilding the payload.';

COMMENT ON COLUMN data_version.version IS
    'Monotonic. Compared, never interpreted -- its value carries no meaning '
    'beyond being different from the last one.';

-- Seeded rather than created on demand, so the read path is a plain SELECT
-- with no "insert it if missing" branch to get wrong under concurrency.
INSERT INTO data_version (scope) VALUES ('journal')
ON CONFLICT (scope) DO NOTHING;


-- Takes the scope as a trigger argument so one function serves every table,
-- and a second scope later (investments, say) needs no new function.
--
-- Returns NULL because an AFTER STATEMENT trigger's return value is discarded;
-- returning NEW would be meaningless here, there being no row in scope.
CREATE OR REPLACE FUNCTION bump_data_version() RETURNS trigger AS $$
BEGIN
    UPDATE data_version
       SET version    = version + 1,
           changed_at = NOW()
     WHERE scope = TG_ARGV[0];
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION bump_data_version() IS
    'Statement-level trigger function. Increments data_version for the scope '
    'named in its first trigger argument.';


-- Attached in a loop rather than eight near-identical statements, so adding a
-- table later is one array entry and cannot drift from the others.
--
-- to_regclass guards each one: this migration must not fail on a database
-- where an unrelated table is absent, and it returns NULL rather than raising
-- for a name that does not exist.
DO $$
DECLARE
    target TEXT;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        -- Read by the equity curve, core stats, the heatmap and the journal.
        'positions',
        'realized_legs',
        -- Read by the journal and by advanced analytics.
        'trades',
        'position_fills',
        'planned_trades',
        -- Names and answers that analytics groups by; renaming one changes
        -- the payload without touching a single position.
        'strategies',
        'disciplines',
        'position_disciplines'
    ]
    LOOP
        IF to_regclass(target) IS NULL THEN
            RAISE NOTICE 'skipping %, table not present', target;
            CONTINUE;
        END IF;

        EXECUTE format(
            'DROP TRIGGER IF EXISTS %I ON %I',
            target || '_bump_data_version', target
        );

        -- TRUNCATE is deliberately not listed: Postgres forbids combining it
        -- with row-level events on one trigger, and nothing in this codebase
        -- truncates these tables.
        EXECUTE format(
            'CREATE TRIGGER %I AFTER INSERT OR UPDATE OR DELETE ON %I '
            'FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version(%L)',
            target || '_bump_data_version', target, 'journal'
        );
    END LOOP;
END $$;

COMMIT;

-- Migration 034: one row per broker sync, so a run that happened is a fact
-- rather than a memory.
--
-- THE PROBLEM
--
-- Nothing records that a sync ran. `LastSyncState` lives in the browser's
-- React Query cache (src/hooks/useTradeInbox.ts, useLastSync) and is written
-- only by the mutation that performs the sync, so it is per-tab, in-memory,
-- and gone on reload. The header badge reads it, which means the badge can
-- only ever describe a sync THIS tab performed.
--
-- That was survivable while every sync was a button press. It stops being
-- survivable the moment one runs on a schedule: a nightly job that fails from
-- an expired token produces exactly what a quiet market produces -- no new
-- fills, no badge, no signal of any kind. The two are indistinguishable, and
-- the one that needs attention is the one that looks like nothing happened.
--
-- WHAT THIS DOES
--
-- Records every run: when, what triggered it, how it ended, and the payload
-- that was reported. The badge then reads the LEDGER's opinion of the last
-- sync instead of the tab's, which is what lets a scheduled run be visible at
-- all, and what lets "nothing has succeeded in three days" be something the
-- app can say out loud.
--
-- WHY A FAILED RUN IS THE IMPORTANT ROW
--
-- The obvious shape -- write the row once the sync succeeds -- loses exactly
-- the runs worth having. A run that raised is the one you need evidence of,
-- so main.py records on every path including the exception ones, and does it
-- in its OWN transaction: a row written inside the ingest transaction
-- disappears when that transaction rolls back, which is precisely the case it
-- was meant to document.
--
-- WHY THE PAYLOAD IS JSONB AND THE COUNTERS ARE NOT
--
-- The four counters below are the ones a human scans and a staleness check
-- reads, so they are columns. The rest of IngestResult is kept whole in
-- `result` because it is a record of what was REPORTED at the time, not
-- something anything aggregates, filters or joins on -- and because that shape
-- has changed twice already (flex_failures, stranded_fills). A JSONB blob
-- absorbs the next change; sixteen columns would need a migration each time.

CREATE TABLE IF NOT EXISTS sync_runs (
    id                UUID PRIMARY KEY,

    started_at        TIMESTAMPTZ NOT NULL,
    -- NULL means the run never reached its own recording step -- the process
    -- died mid-flight. Distinct from a run that failed and said so.
    finished_at       TIMESTAMPTZ,

    -- What set it off. The entire reason this table exists is to tell a
    -- schedule that ran from a schedule that never fired, so this is NOT NULL
    -- and constrained rather than free text.
    trigger           TEXT NOT NULL,

    -- 'partial' is its own outcome, not a flavour of success: it means some
    -- Flex queries did not return, so the ledger is short of fills that exist
    -- at the broker. Collapsing it into 'success' is how a half-empty sync
    -- comes to look complete.
    outcome           TEXT NOT NULL,

    executions_parsed INTEGER NOT NULL DEFAULT 0,
    trades_created    INTEGER NOT NULL DEFAULT 0,
    positions_matched INTEGER NOT NULL DEFAULT 0,
    plans_attached    INTEGER NOT NULL DEFAULT 0,

    -- The full IngestResult as sent to the browser. NULL when the run raised
    -- before producing one.
    result            JSONB,
    -- Set only when the run raised. Carries the detail the user would have
    -- seen in the toast, so a failure is diagnosable after the fact.
    error             TEXT,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Mirrors the values main.py writes. Same reasoning as planned_trades.status:
    -- a typo becomes a loud constraint violation at write time instead of a
    -- value that quietly matches no filter for the rest of the table's life.
    CONSTRAINT sync_runs_trigger_check
        CHECK (trigger IN ('manual', 'cron')),
    CONSTRAINT sync_runs_outcome_check
        CHECK (outcome IN ('success', 'partial', 'error'))
);

-- Every read of this table is "the most recent run", or "the most recent
-- SUCCESSFUL run" for the staleness check. Both walk this index backwards.
CREATE INDEX IF NOT EXISTS sync_runs_started_at_idx
    ON sync_runs (started_at DESC);

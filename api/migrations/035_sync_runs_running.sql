-- Migration 035: a sync run can be IN PROGRESS, not just finished
--
-- THE PROBLEM
--
-- POST /api/ingest/ibkr ran the whole two-step IBKR Flex handshake inline and
-- returned when it was done. That handshake is 15 to 240 seconds -- IBKR
-- compiles the statement on its own schedule and the client polls for it -- so
-- pressing Sync froze the button for as long as the broker felt like taking,
-- with no progress and no way to navigate away and come back to an answer.
--
-- Making that asynchronous needs somewhere to put "this run exists and has not
-- finished". `sync_runs` is already exactly the right table -- it records every
-- run and the header badge already reads it -- except that `outcome` is
-- constrained to three TERMINAL values. A row is only written once the run is
-- over, so an in-flight run is indistinguishable from one that never started.
--
-- WHAT THIS DOES
--
-- Adds 'running' to the allowed outcomes. A run now writes its row up front and
-- updates it on the way out, which buys three things at once:
--
--   * the browser can be handed a 202 and a run id, then follow the row;
--   * a second Sync press can be refused, because an in-flight run is visible
--     rather than inferred;
--   * a run killed mid-flight leaves evidence. Previously a container restart
--     during an ingest left NOTHING in this table -- the row was only written
--     at the end -- so the most interesting failure was the one that recorded
--     itself least.
--
-- WHAT 'running' IS NOT
--
-- It is not a success and not a failure, and nothing that measures the ledger
-- may treat it as either. The staleness warning reads the most recent
-- SUCCESSFUL run, so a run stuck in 'running' can never make the journal look
-- fresher than it is -- which is the failure mode that matters, and the reason
-- this is a fourth value rather than a nullable `finished_at` being overloaded
-- to mean it.
--
-- Rows can strand: a worker recycled mid-ingest leaves 'running' behind with
-- nobody to finish it. That is reaped in application code rather than here,
-- against the same fetch budget the ingest is bounded by -- a CHECK constraint
-- cannot express "older than the timeout", and a database-side job would be a
-- second scheduler to keep alive.

BEGIN;

ALTER TABLE sync_runs
    DROP CONSTRAINT IF EXISTS sync_runs_outcome_check;

ALTER TABLE sync_runs
    ADD CONSTRAINT sync_runs_outcome_check
        CHECK (outcome IN ('running', 'success', 'partial', 'error'));

COMMENT ON COLUMN sync_runs.outcome IS
    'running | success | partial | error. ''running'' is written when the run '
    'starts and replaced when it ends; it is neither a success nor a failure, '
    'and the staleness check reads only ''success'' so a stranded run cannot '
    'make the ledger look fresher than it is.';

-- Both new reads are "is anything running right now" -- the 409 guard on a
-- second Sync press, and the reaper looking for stranded rows. A partial index
-- keeps those off the main started_at index and stays tiny: it holds at most
-- one row in normal operation, and zero most of the time.
CREATE INDEX IF NOT EXISTS sync_runs_running_idx
    ON sync_runs (started_at DESC)
    WHERE outcome = 'running';

COMMIT;

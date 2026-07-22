-- Migration 016: make the execution ledger correctable by hand.
--
-- The ledger has been treated as append-only: fills arrive from the broker or
-- from manual entry, and the only correction available is deletion. That was
-- adequate while the broker was assumed correct. It is not, for two reasons
-- this journal has now hit in practice.
--
-- WHY DELETION ALONE DOES NOT WORK
--
-- Deleting a broker fill does not stick. Ingest is idempotent through
-- `ON CONFLICT (ibkr_exec_id) DO NOTHING`, which skips rows that still exist
-- -- a deleted row no longer exists, so the next sync cheerfully re-inserts
-- it. The delete button would appear to work and then silently undo itself.
--
-- `suppressed_executions` is the tombstone that makes deletion permanent. It
-- stores the broker id rather than a foreign key precisely because the row it
-- refers to is gone; a FK would be deleted along with it and suppress nothing.
--
-- WHY EDITING NEEDS A RECORD
--
-- Once a fill can be hand-edited, the ledger can silently stop matching the
-- broker, and reconciliation -- the thing that caught the duplicate-sync bug
-- -- becomes impossible to trust. So the first edit snapshots what the broker
-- originally said. The ledger stays correctable AND stays honest about which
-- figures are no longer the broker's.

-- --------------------------------------------------------------------------
-- 1. Tombstones for deleted broker fills.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS suppressed_executions (
    -- The broker's own id, e.g. 'IBKR-111634108'. Deliberately not a FK: the
    -- trade it names has been deleted, which is the entire point.
    ibkr_exec_id  TEXT PRIMARY KEY,
    ticker        TEXT,
    -- Free text, so a future reader knows whether this was a duplicate, a
    -- cancelled trade, or a mistake.
    reason        TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Ingest checks this on every promotion, so it is read far more than written.
CREATE INDEX IF NOT EXISTS ix_suppressed_executions_ticker
    ON suppressed_executions (ticker);

-- --------------------------------------------------------------------------
-- 2. Provenance for hand-edited fills.
-- --------------------------------------------------------------------------
-- NULL edited_at means "untouched since it arrived", which is the honest
-- default for the 326 rows already here. A timestamp rather than a boolean:
-- knowing a fill was corrected is useful, knowing when is more useful.
ALTER TABLE trades ADD COLUMN IF NOT EXISTS edited_at TIMESTAMPTZ;

-- What the broker said before the first edit: {quantity, actual_entry,
-- entry_date, direction}. Written once and never overwritten, so a second edit
-- does not snapshot the first edit's values and lose the original.
--
-- JSONB rather than four shadow columns. These are never filtered or joined
-- on -- they exist to be displayed next to the current value and to prove
-- reconciliation -- so a document is the honest shape, and it does not widen
-- the hot table with four columns that are NULL on almost every row.
ALTER TABLE trades ADD COLUMN IF NOT EXISTS broker_original JSONB;

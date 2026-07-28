-- Migration 019: a round trip may not outlive the executions it was built from
--
-- `positions.open_trade_id` / `close_trade_id` are ON DELETE SET NULL. That
-- was chosen so deleting an execution could never cascade into deleting the
-- trader's review -- but it means a position can end up asserting a quantity,
-- a weighted entry and a realised P&L with no executions underneath it, and
-- nothing in the schema says that is wrong.
--
-- It is worse than an ordinary dangling reference. uq_positions_open_close is
-- what makes re-running the matching engine idempotent, and Postgres treats
-- NULLs as distinct in a unique index -- the trap migration 001 warns about in
-- its own comment. A row keyed (NULL, ...) collides with nothing, so the next
-- rebuild inserts the same round trip again with real ids: two rows, one
-- trade, P&L counted twice.
--
-- Every application path already prevents this by deleting the affected
-- positions BEFORE the executions (delete_trade, update_execution,
-- delete_position). This constraint is the backstop for the path someone adds
-- later and forgets. With SET NULL still in place, such a delete now fails
-- loudly at the database instead of silently corrupting the ledger.
--
-- Safe to apply: verified against the live database beforehand -- 0 of 124
-- positions had a NULL key, none pointed at a missing trade, and every
-- position's open/close execution had a matching position_fills row, so the
-- trade -> positions lookup those paths rely on is complete.
--
-- Deliberately NOT a partial unique index on (open_trade_id, close_trade_id)
-- WHERE both are NOT NULL. uq_positions_open_close already covers exactly
-- that, and a WHERE ... IS NOT NULL predicate excludes the NULL rows it would
-- be meant to constrain -- it cannot stop (NULL, NULL) accumulating, because
-- those are precisely the rows it declines to index.

BEGIN;

-- Fail before altering anything if the assumption above no longer holds, so a
-- future run against different data reports the reason rather than an opaque
-- constraint violation.
DO $$
DECLARE
    orphaned INTEGER;
BEGIN
    SELECT count(*) INTO orphaned
    FROM positions
    WHERE open_trade_id IS NULL OR close_trade_id IS NULL;

    IF orphaned > 0 THEN
        RAISE EXCEPTION
            '% position(s) have a NULL open/close trade id. They are round '
            'trips whose executions were deleted out from under them; re-run '
            'matching for their tickers (or delete them) before applying this.',
            orphaned;
    END IF;
END $$;

ALTER TABLE positions
    ALTER COLUMN open_trade_id  SET NOT NULL,
    ALTER COLUMN close_trade_id SET NOT NULL;

COMMIT;

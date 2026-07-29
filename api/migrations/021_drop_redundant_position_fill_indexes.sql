-- Migration 021: drop both standalone indexes on position_fills(position_id)
--
-- The table ended up carrying three indexes that answer the same question.
-- Migration 009 created ix_position_fills_position, migration 012 created
-- ix_position_fills_position_id, and neither noticed the other -- so the two
-- files disagree about which one owns the lookup, and the database has both.
--
-- Both are redundant, not just the duplicate. uq_position_fills_position_trade_role
-- is (position_id, trade_id, role), and a B-tree serves any query on a leading
-- subset of its columns, so it already answers `WHERE position_id = ?` on its
-- own. That is the only shape either standalone index was created for:
--
--     list_position_fills   WHERE position_id = ?
--     delete_position       WHERE position_id = ?
--     _sync_position_fills  WHERE position_id IN (...)
--
-- Honest about the size of this. The table holds ~318 rows; the write
-- amplification and the duplicated cache footprint are somewhere between
-- microseconds and unmeasurable. This is not a performance fix, it is the
-- schema and the migration files agreeing on what exists. The reason to do it
-- as a migration rather than a hand-run DROP is that migrations are meant to
-- rebuild this schema from nothing -- a manual drop is undone the next time
-- 009 is replayed.
--
-- Recorded, because it is the interesting part: pg_stat_user_indexes showed
-- ix_position_fills_position_id with 1854 scans, so the planner had been
-- choosing it over the composite -- it is 40 kB against the composite's 80 kB
-- and cheaper to walk for a position_id-only predicate. Those scans move to
-- the composite, which is a marginally larger index for the same result. At
-- this row count that is not a trade worth measuring; at a million rows it
-- would be, and the answer would be to keep ONE standalone index rather than
-- to bring back both.
--
-- DROP INDEX takes ACCESS EXCLUSIVE on the table. Microseconds here. If this
-- table is ever large enough for that to matter, DROP INDEX CONCURRENTLY does
-- the same job without the lock -- but it cannot run inside a transaction, so
-- it would have to come out of this block.

BEGIN;

DROP INDEX IF EXISTS ix_position_fills_position;      -- migration 009
DROP INDEX IF EXISTS ix_position_fills_position_id;   -- migration 012

COMMIT;

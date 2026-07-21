-- Migration 011: the journalling half of a trading journal.
--
-- Until now a trade could record what happened but not why. The only free-text
-- field was `positions.notes`, written after the fact, which cannot capture the
-- reasoning that existed at entry -- and reconstructing a thesis after seeing
-- the outcome is exactly the bias a journal exists to defeat.
--
--   trades.thesis            why this trade was taken, written at entry
--   positions.review_*       the post-mortem, split into three questions
--                            rather than one undifferentiated notes box
--
-- The split matters: "what went well / what went wrong / what to learn" are
-- different questions, and a single box reliably collapses into only the
-- middle one. Kept as separate columns so they stay separately reviewable.
--
-- `positions.notes` is retained. It predates this and holds existing data;
-- nothing is migrated or dropped.

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS thesis TEXT;

ALTER TABLE positions
    ADD COLUMN IF NOT EXISTS review_went_well  TEXT,
    ADD COLUMN IF NOT EXISTS review_went_wrong TEXT,
    ADD COLUMN IF NOT EXISTS review_lessons    TEXT;

-- The master list filters and sorts the ledger by recency.
CREATE INDEX IF NOT EXISTS ix_trades_entry_date ON trades (entry_date DESC);

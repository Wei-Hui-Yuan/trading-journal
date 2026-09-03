-- Migration 038: how long a round trip sits before it is actually journaled.
--
-- `review_status` already says whether a position has left the Trade Inbox
-- queue, but it cannot say WHEN, and it cannot say WHY -- 'reviewed' is the
-- same value whether the trader wrote a real post-mortem or dismissed the
-- trade with nothing to say about it (`dismiss_position`, migration-free,
-- reuses the exact same status for both). A "time to journal" metric built
-- on `review_status` alone would silently reward dismissing over writing,
-- which is the opposite of what the metric is for.
--
-- This column is the answer to a narrower question: the moment a GENUINE
-- review completed. Set exactly once, by `review_position` the first time
-- `mark_reviewed` actually transitions the position -- never re-stamped by a
-- later edit, and never touched by `dismiss_position` at all. NULL means
-- "not journaled yet," which is a real and common state, not a zero.

ALTER TABLE positions
    ADD COLUMN IF NOT EXISTS journaled_at TIMESTAMPTZ;

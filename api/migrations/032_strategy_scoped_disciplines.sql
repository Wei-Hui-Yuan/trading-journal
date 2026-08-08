-- Migration 032: a discipline rule may belong to one strategy, or to none
--
-- THE PROBLEM
--
-- Every discipline rule is global today -- "Hard stop-loss set" is checked on
-- every reviewed trade regardless of what was traded. That is right for a
-- rule about behaviour, and wrong for a rule about a specific setup: "Price
-- reclaimed the prior day high on volume" only means something on a trade
-- actually following that playbook entry, and two unrelated strategies can
-- never both use a rule named "Waited for the retest" -- disciplines.name is
-- UNIQUE across the whole table.
--
-- WHAT THIS DOES
--
-- Adds a nullable strategy_id to disciplines. NULL keeps meaning what it
-- already means -- a general rule, checked on every trade. Set, it scopes the
-- rule to one playbook entry, and the review checklist (main.py's
-- list_disciplines callers) is expected to show a trade only the rules that
-- apply to it: every general rule, plus whichever strategy it is tagged
-- with.
--
-- WHY EXTEND disciplines RATHER THAN A SEPARATE TABLE
--
-- position_disciplines, compute_discipline_score and compute_compliance_
-- buckets (migration 030's counter included) all already work by reading
-- whatever discipline rows exist for a position, with no notion of where a
-- rule came from. A second, parallel checklist table would need its own
-- answer table, its own scoring path, and compliance_buckets would need to
-- merge two sources to give one honest number. Adding one nullable column
-- keeps exactly one checklist system, and every downstream computation stays
-- correct with zero changes to its own logic.
--
-- ON DELETE CASCADE, not SET NULL
--
-- SET NULL would silently turn a strategy-specific rule into a general one
-- the moment its strategy is deleted -- "Price reclaimed the prior day high"
-- would start being asked on every trade of every other strategy, which is
-- not a fallback, it is a different rule wearing the old one's name. CASCADE
-- removes the rule (and, via position_disciplines' own existing CASCADE, the
-- historical answers to it) along with the strategy it has no meaning
-- without. The trades, positions and plans that referenced the strategy are
-- unaffected -- those three foreign keys stay ON DELETE SET NULL, exactly as
-- migration 002 and 018 left them; only the checklist, which is opinion
-- rather than a record of what happened, disappears with its strategy.
--
-- TWO PARTIAL INDEXES, NOT ONE COMPOSITE UNIQUE
--
-- Postgres does not treat two NULLs as equal for uniqueness, so a plain
-- UNIQUE(strategy_id, name) would silently allow the same general-rule name
-- twice (two rows with strategy_id NULL are never "duplicates" to it) -- the
-- exact trap migration 001 already documents for positions' own open/close
-- pairing. Splitting the constraint in two closes it for both halves at once:
-- one partial index enforces "one name per NULL", the other "one name per
-- strategy".

BEGIN;

ALTER TABLE disciplines
    ADD COLUMN IF NOT EXISTS strategy_id UUID REFERENCES strategies(id) ON DELETE CASCADE;

-- The blanket UNIQUE(name) this replaces would otherwise keep rejecting the
-- exact thing this migration exists to allow: the same rule name reused
-- under two different strategies.
ALTER TABLE disciplines
    DROP CONSTRAINT IF EXISTS disciplines_name_key;

CREATE UNIQUE INDEX IF NOT EXISTS uq_disciplines_general_name
    ON disciplines (name)
    WHERE strategy_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_disciplines_strategy_name
    ON disciplines (strategy_id, name)
    WHERE strategy_id IS NOT NULL;

-- The review checklist has to know which trades a strategy-scoped rule even
-- applies to; every position/plan lookup filtering "does this trade's
-- strategy have rules" runs through this.
CREATE INDEX IF NOT EXISTS ix_disciplines_strategy_id
    ON disciplines (strategy_id);

COMMIT;

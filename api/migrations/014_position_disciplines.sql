-- Migration 014: disciplines become a real relation.
--
-- Migration 013 made the RULES editable but left the ANSWERS hardcoded.
-- `positions` carried three fixed booleans -- tag_hard_sl, tag_retest,
-- tag_plan_compliant -- and the review UI mapped them by matching three
-- string literals against the rule names. A rule the user added was rendered
-- as a checkbox, held in React state, and silently dropped on save: there was
-- nowhere in the schema to put the answer. Renaming a default rule broke its
-- column mapping just as quietly.
--
-- A join table fixes both, and makes the data askable. The question worth
-- asking is not "did I follow my plan on this trade" but "what is my win rate
-- when I follow my plan", and that is a GROUP BY, not a boolean column.
--
-- FOLLOWED vs NOT RECORDED
--
-- The absence of a row means "not reviewed against this rule". A row with
-- followed = FALSE means "reviewed, and I did not follow it". These are
-- deliberately different, for the same reason compute_r_multiple returns NULL
-- rather than 0.0 when a trade cannot be scored: folding "unknown" into
-- "no" would quietly drag every discipline's compliance rate toward zero and
-- make an unreviewed backlog look like indiscipline.
--
-- The three legacy columns are RETAINED, not dropped -- the same choice
-- migration 011 made for positions.notes. They are backfilled below and then
-- superseded; nothing reads them for new work, but existing rows keep their
-- history and a rollback does not lose data.

-- --------------------------------------------------------------------------
-- 0. Repair the table 013 was supposed to create.
-- --------------------------------------------------------------------------
-- On the live database `disciplines` was not created by migration 013 at all.
-- It was created by a Base.metadata.create_all at boot (since reverted -- DDL
-- reflection is unsupported on Supabase's transaction-mode pooler). The ORM
-- declares `default=uuid.uuid4`, which is generated in PYTHON, so create_all
-- emitted no column default at all: `disciplines.id` had none, while every
-- hand-migrated table has gen_random_uuid().
--
-- The API never noticed, because SQLAlchemy supplies the UUID itself. Any raw
-- SQL INSERT fails outright with a not-null violation on id -- including the
-- seed below, and including migration 013 if it were run now (its CREATE TABLE
-- IF NOT EXISTS would no-op against the existing table, then its INSERT would
-- fail). Repaired first so the schema matches the rest of the database.
ALTER TABLE disciplines ALTER COLUMN id SET DEFAULT gen_random_uuid();

-- --------------------------------------------------------------------------
-- 1. Seed the default rules.
-- --------------------------------------------------------------------------
-- create_all creates schema but never data, so the table existed and was
-- empty: the review panel showed no rules at all.
INSERT INTO disciplines (name) VALUES
    ('Hard stop-loss set'),
    ('Waited for retest'),
    ('Followed the plan')
ON CONFLICT (name) DO NOTHING;

-- --------------------------------------------------------------------------
-- 2. The join table.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS position_disciplines (
    position_id   UUID NOT NULL REFERENCES positions(id)   ON DELETE CASCADE,
    discipline_id UUID NOT NULL REFERENCES disciplines(id) ON DELETE CASCADE,
    followed      BOOLEAN NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    -- One answer per rule per round trip. Also the ON CONFLICT target that
    -- makes re-saving a review an update instead of a duplicate.
    PRIMARY KEY (position_id, discipline_id)
);

-- CASCADE on discipline_id is intentional: deleting a rule the user no longer
-- believes in should not leave its answers behind to skew a breakdown that no
-- longer has a name for them.

-- The analytics breakdown groups by discipline and joins back to positions;
-- the reverse lookup drives the review panel for a single position.
CREATE INDEX IF NOT EXISTS ix_position_disciplines_discipline
    ON position_disciplines (discipline_id);

-- --------------------------------------------------------------------------
-- 3. Backfill the three legacy columns.
-- --------------------------------------------------------------------------
-- Only REVIEWED positions are backfilled. An unreviewed row has tag_* = FALSE
-- by column default, which means "nobody has answered yet" and must not be
-- recorded as "did not follow" -- that is exactly the unknown/no conflation
-- this table exists to avoid.
INSERT INTO position_disciplines (position_id, discipline_id, followed)
SELECT p.id, d.id,
       CASE d.name
           WHEN 'Hard stop-loss set' THEN COALESCE(p.tag_hard_sl, FALSE)
           WHEN 'Waited for retest'  THEN COALESCE(p.tag_retest, FALSE)
           WHEN 'Followed the plan'  THEN COALESCE(p.tag_plan_compliant, FALSE)
       END
FROM positions p
CROSS JOIN disciplines d
WHERE p.review_status = 'reviewed'
  AND d.name IN ('Hard stop-loss set', 'Waited for retest', 'Followed the plan')
ON CONFLICT (position_id, discipline_id) DO NOTHING;

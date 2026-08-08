-- Migration 033: pre-trade checklist answers, one row per plan per rule.
--
-- THE PROBLEM
--
-- position_disciplines (migration 014) answers "did this round trip follow
-- rule X", filled in during the post-trade review -- after the outcome is
-- known. A plan (planned_trades, migration 018) has no equivalent: its
-- Strategy field already shows which checklist applies, but there is nowhere
-- to record an answer before the trade happens, which is the only point in
-- a plan's life it is safe to ask "will I follow this" rather than "did I".
--
-- WHAT THIS DOES
--
-- A join table shaped exactly like position_disciplines, but keyed on
-- plan_id instead of position_id. main.py's list_positions merges a pending
-- position's origin plan (found via positions.open_trade_id -> trades.id ->
-- trades.plan_id) into the checklist it returns, for any rule the review has
-- not already answered for real -- so the Trade Inbox opens with the plan's
-- answers pre-ticked instead of blank.
--
-- WHY THIS DOES NOT FEED ANALYTICS
--
-- compute_discipline_score and compute_compliance_buckets (services/
-- analytics.py) read position_disciplines exclusively, and nothing here
-- changes that. A plan-time answer is intent, not behaviour -- a trader can
-- tick every box before entering and still abandon the plan mid-trade, and
-- scoring that as compliance would measure the plan instead of the trade.
-- The moment a pre-filled checklist is actually saved through PUT/PATCH
-- /api/positions/{id}/review, though, it becomes a real position_disciplines
-- row like any other answer, indistinguishable from one ticked from scratch
-- -- which is the point: this table only ever supplies a starting value for
-- that save, never a second score alongside it.
--
-- ON DELETE CASCADE, matching position_disciplines
--
-- A plan's checklist has no meaning once the plan itself is gone, and a rule
-- deleted from disciplines should take every answer to it with it, plan-time
-- and post-trade alike -- the same reasoning migration 014 gives for
-- position_disciplines.

BEGIN;

CREATE TABLE IF NOT EXISTS plan_disciplines (
    plan_id       UUID NOT NULL REFERENCES planned_trades(id) ON DELETE CASCADE,
    discipline_id UUID NOT NULL REFERENCES disciplines(id)    ON DELETE CASCADE,
    followed      BOOLEAN NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (plan_id, discipline_id)
);

-- Mirrors position_disciplines' own index: deleting a rule needs its answers
-- found by discipline_id, not just by plan_id.
CREATE INDEX IF NOT EXISTS ix_plan_disciplines_discipline
    ON plan_disciplines (discipline_id);

COMMIT;

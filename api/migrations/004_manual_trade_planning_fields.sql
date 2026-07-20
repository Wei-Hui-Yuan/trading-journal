-- Migration 004: planning & risk fields for manual trade logging
--
-- IMPORTANT -- READ BEFORE ADDING COLUMNS.
--
-- The planning fields the manual-entry form collects ALREADY EXIST on the
-- `trades` ledger, two of them under different names:
--
--     form field           ->  trades column
--     -------------------      ----------------------------
--     Planned Entry        ->  planned_entry   NUMERIC(10,4)
--     Planned Stop Loss    ->  stop_loss       NUMERIC(10,4)
--     Take Profit Price    ->  target          NUMERIC(10,4)
--     Actual Entry Price   ->  actual_entry    NUMERIC(10,4)  (NOT NULL)
--     Exit Price           ->  exit_price      NUMERIC(10,4)
--
-- Adding `planned_stop_loss` / `take_profit_price` as NEW columns would split
-- one concept across two fields: PUT /api/trades/{id} already writes the
-- planned stop to `stop_loss` and the target to `target`, so the review flow
-- and the manual-entry flow would disagree about where a stop lives.
--
-- This migration therefore only GUARANTEES the canonical columns exist. On the
-- current database every statement is a no-op; it matters only when
-- bootstrapping an environment from an older schema dump.
--
-- All columns are nullable: broker-synced fills and pre-existing rows carry no
-- plan, and must keep working.

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS planned_entry NUMERIC(10, 4),
    ADD COLUMN IF NOT EXISTS stop_loss     NUMERIC(10, 4),
    ADD COLUMN IF NOT EXISTS target        NUMERIC(10, 4),
    ADD COLUMN IF NOT EXISTS exit_price    NUMERIC(10, 4);

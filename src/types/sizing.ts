import type { TradeSide } from './api';

/**
 * One note in the sizing scratchpad -- a fast, deliberately incomplete
 * record of a trade being considered, for the moment there is no time to
 * open the Plan modal and write a real plan.
 *
 * Nothing here is read by the matching engine or analytics. Promoting an
 * entry (`POST /api/sizing-scratchpad/{id}/promote`) is the only bridge to
 * `TradePlan`, and it is one-way.
 */
export interface SizingScratchpadEntry {
  id: string;
  ticker: string;
  direction: TradeSide;
  entry: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  quantity: number | null;
  created_at: string; // ISO 8601
}

/** POST /api/sizing-scratchpad. Only ticker and direction are required. */
export interface SizingEntryPayload {
  ticker: string;
  direction: TradeSide;
  entry?: number | null;
  stop_loss?: number | null;
  take_profit?: number | null;
  quantity?: number | null;
}

/** PATCH /api/sizing-scratchpad/{id}. Only keys present are applied. */
export type SizingEntryUpdatePayload = Partial<SizingEntryPayload>;

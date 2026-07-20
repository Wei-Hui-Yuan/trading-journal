/**
 * Types mirroring the FastAPI backend response models.
 *
 * Kept separate from `types/trade.ts`, which describes the older raw-execution
 * (`trades` table) shape. This file covers the positions/strategies/analytics
 * layer the UI is being rebuilt on.
 *
 * Field names match the API payloads exactly (snake_case) so responses can be
 * consumed without a mapping step.
 */

// ---------------------------------------------------------------------------
// Strategies
// ---------------------------------------------------------------------------

export interface Strategy {
  id: string; // UUID
  name: string;
  description: string | null;
  /** Overarching approach, e.g. "Supply/Demand", "Momentum Breakout". */
  method: string;
  /** Entry rules / triggers, free-form (newline-separated bullets). */
  entry_criteria: string;
  /** Exit rules, risk parameters, targets. */
  exit_criteria: string;
  created_at: string | null; // ISO 8601
}

export interface StrategyCreatePayload {
  name: string;
  description?: string | null;
  method?: string;
  entry_criteria?: string;
  exit_criteria?: string;
}

/**
 * Body for PATCH /api/strategies/{id}.
 *
 * Only keys present are applied server-side, so saving one field never clears
 * the others.
 */
export interface StrategyUpdatePayload {
  name?: string;
  description?: string | null;
  method?: string;
  entry_criteria?: string;
  exit_criteria?: string;
}

// ---------------------------------------------------------------------------
// Positions
// ---------------------------------------------------------------------------

/** Lifecycle of a position in the Trade Inbox. */
export type ReviewStatus = 'pending' | 'completed';

/** Holding-period buckets assigned by the FIFO matching engine. */
export type TradeStyle = 'Scalp' | 'Day Trade' | 'Swing Trade';

/**
 * A closed round trip from the `positions` table.
 *
 * Numeric columns arrive as JSON numbers (FastAPI widens Decimal to float at
 * the response boundary), so they are `number` here, not `string`.
 */
export interface Position {
  id: string; // UUID
  symbol: string;
  style: TradeStyle | string;
  quantity: number;
  entry_price: number;
  exit_price: number;
  entry_time: string; // ISO 8601, UTC
  exit_time: string; // ISO 8601, UTC
  realized_pnl: number;

  // Review workflow (migration 002)
  strategy_id: string | null;
  review_status: ReviewStatus | null;
  tag_hard_sl: boolean | null;
  tag_retest: boolean | null;
  tag_plan_compliant: boolean | null;
  trade_grade: string | null;

  created_at: string | null;
}

/**
 * Body for PATCH /api/positions/{id}/review.
 *
 * Every field is optional: the backend applies only keys present in the
 * request (`exclude_unset`), so a partial save never clears untouched fields.
 * Submitting this always flips `review_status` to 'completed' server-side.
 */
export interface PositionReviewPayload {
  strategy_id?: string | null;
  tag_hard_sl?: boolean;
  tag_retest?: boolean;
  tag_plan_compliant?: boolean;
  trade_grade?: string | null;
}

// ---------------------------------------------------------------------------
// IBKR ingestion
// ---------------------------------------------------------------------------

/**
 * Result of POST /api/ingest/ibkr.
 *
 * Reports each stage of the pipeline separately, so a run that finds nothing
 * new is distinguishable from one that failed. `staged_duplicates` being high
 * with `staged_new` at 0 is the normal, healthy outcome of re-syncing an
 * overlapping date range.
 */
export interface IngestResult {
  executions_parsed: number;
  /** Fills new to the staging ledger this run. */
  staged_new: number;
  /** Fills rejected by the transaction_id UNIQUE guard — already ingested. */
  staged_duplicates: number;
  trades_created: number;
  trades_duplicates: number;
  positions_matched: number;
  symbols_touched: string[];
  /** Fractional fills rounded to satisfy the INTEGER quantity column. */
  fractional_quantities: number;
}

// ---------------------------------------------------------------------------
// Manual trade entry
// ---------------------------------------------------------------------------

export type TradeSide = 'BUY' | 'SELL';

/**
 * Body for POST /api/trades/manual.
 *
 * `quantity` must be a whole number — `trades.quantity` is an INTEGER column,
 * and the API rejects fractional shares rather than truncating them.
 *
 * `execution_time` omitted means "now, US market time". A value without a
 * timezone offset is interpreted by the backend as America/New_York.
 */
export interface ManualTradePayload {
  symbol: string;
  side: TradeSide;
  quantity: number;
  /** The fill actually received. Maps to trades.actual_entry. */
  price: number;
  execution_time?: string | null;

  // Planning / risk setup. All optional — send null, never '' or NaN.
  // Server-side these land on existing ledger columns:
  //   planned_stop_loss -> trades.stop_loss
  //   take_profit_price -> trades.target
  planned_entry?: number | null;
  planned_stop_loss?: number | null;
  take_profit_price?: number | null;
  /** Left null while the trade is still running. */
  exit_price?: number | null;
}

export interface ManualTradeResult {
  trade_id: string;
  ticker: string;
  direction: TradeSide;
  quantity: number;
  price: number;
  execution_time: string;
  planned_entry: number | null;
  planned_stop_loss: number | null;
  take_profit_price: number | null;
  exit_price: number | null;
  /** Round trips the FIFO engine closed because of this execution. */
  positions_created: number;
  /** Shares left open on this ticker after matching. */
  open_quantity: number;
}

// ---------------------------------------------------------------------------
// Analytics
// ---------------------------------------------------------------------------

export type SessionName = 'Morning' | 'Midday' | 'Afternoon' | 'After-Hours';

export type DayName =
  | 'Monday'
  | 'Tuesday'
  | 'Wednesday'
  | 'Thursday'
  | 'Friday';

export interface CoreStats {
  net_pnl: number;
  win_rate_pct: number;
  total_trades: number;
  /**
   * Gross wins / gross losses.
   *
   * `null` when there are no losing trades — the ratio is unbounded and has no
   * finite value. The backend deliberately sends null rather than Infinity,
   * which is not valid JSON. Always null-check before formatting.
   */
  profit_factor: number | null;
  avg_roi_pct: number;
}

/** One cell of the day x session heatmap. */
export interface HeatmapCell {
  trade_count: number;
  net_pnl: number;
  win_rate_pct: number;
  /**
   * Gross wins / gross losses for this cell.
   *
   * `null` means the cell contains no losing trades — a flawless session.
   * Not derivable from `win_rate_pct`: a scratch trade (P&L exactly 0) counts
   * as neither win nor loss, so a cell can be loss-free below 100% win rate.
   */
  profit_factor: number | null;
}

export interface HeatmapTotal {
  trade_count: number;
  net_pnl: number;
}

/**
 * Day/session grid.
 *
 * Every day/session combination is pre-seeded by the backend, so
 * `cells[day][session]` is always defined — no null checks needed when
 * rendering the matrix.
 */
export interface Heatmap {
  days: DayName[];
  sessions: SessionName[];
  cells: Record<DayName, Record<SessionName, HeatmapCell>>;
  day_totals: Record<DayName, HeatmapTotal>;
  session_totals: Record<SessionName, HeatmapTotal>;
  /** IANA zone the bucketing was performed in (America/New_York). */
  timezone: string;
  /** Positions dropped because they fell on a Saturday or Sunday. */
  excluded_weekend_trades: number;
}

export interface DashboardStats {
  core_stats: CoreStats;
  heatmap: Heatmap;
}

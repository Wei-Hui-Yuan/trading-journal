/**
 * Types mirroring the FastAPI backend response models.
 *
 * The single source of truth for API shapes. Field names match the payloads
 * exactly (snake_case) so responses are consumed without a mapping step; the
 * one exception is `KPIStats` at the bottom, a camelCase presentation shape.
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
// Disciplines
// ---------------------------------------------------------------------------

export interface Discipline {
  id: string; // UUID
  name: string;
  created_at: string | null;
}

export interface DisciplineCreatePayload {
  name: string;
}

/**
 * One rule's answer for one round trip (migration 014).
 *
 * A rule absent from a position's list is UNREVIEWED, which is not the same
 * as `followed: false`. Rendering absence as unchecked is fine; recording it
 * as "did not follow" is not, and would drag every compliance rate down.
 */
export interface PositionDiscipline {
  discipline_id: string;
  name: string;
  followed: boolean;
}

// ---------------------------------------------------------------------------
// Positions
// ---------------------------------------------------------------------------

/** Lifecycle of a position in the Trade Inbox. */
export type ReviewStatus = 'pending' | 'reviewed';

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
  /** Free-form review notes (migration 007, moved here from trades). */
  notes: string | null;
  /** Behavioural tags, e.g. ['FOMO', 'Chased']. */
  mistakes: string[];

  // Post-mortem, asked as three separate questions (migration 011).
  review_went_well: string | null;
  review_went_wrong: string | null;
  review_lessons: string | null;

  /** Answers to the user's own rules. Only rules actually answered appear. */
  disciplines: PositionDiscipline[];

  created_at: string | null;
}

/**
 * One execution behind a position (GET /api/positions/{id}/fills).
 *
 * A position aggregates flat-to-flat, so `entry_price` and `exit_price` above
 * are quantity-weighted averages. These are the individual scale-ins and
 * scale-outs those averages are computed from.
 */
export interface PositionFill {
  id: string; // UUID
  trade_id: string; // UUID of the underlying execution in `trades`
  role: 'OPEN' | 'CLOSE';
  /**
   * Shares attributed to *this* position, which is not always the fill's full
   * size: one execution can close a long and open a short.
   */
  quantity: number;
  price: number;
  executed_at: string; // ISO 8601, UTC
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
  notes?: string | null;
  mistakes?: string[];
  review_went_well?: string | null;
  review_went_wrong?: string | null;
  review_lessons?: string | null;
  exit_reason?: string | null;
  /** Hindsight-optimal levels for THIS trade — scores plan quality. */
  ideal_entry?: number | null;
  ideal_stop?: number | null;
  ideal_target?: number | null;
  /** The corrected rule for the NEXT instance of this setup. */
  revised_entry?: number | null;
  revised_stop?: number | null;
  revised_target?: number | null;
  /**
   * Discipline answers keyed by discipline id. Omit the field to leave
   * existing answers untouched; include a rule with `false` to record
   * "reviewed, did not follow" — a different statement from omitting it.
   */
  disciplines?: Record<string, boolean>;
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
  /**
   * Statement rows that were not tradeable positions — chiefly currency
   * conversions in a multi-currency account, which can outnumber the real
   * fills and would otherwise each become a position.
   */
  skipped_non_tradeable: number;
  /**
   * Queries that did not return on this run. IBKR rate-limits report
   * generation per token and its cooldown outlasts a request, so a sync can
   * legitimately return part of the picture — this says which part is missing
   * rather than letting a partial sync look complete.
   */
  queries_failed: string[];
}

// ---------------------------------------------------------------------------
// Manual trade entry
// ---------------------------------------------------------------------------

export type TradeSide = 'BUY' | 'SELL';

/**
 * Body for POST /api/trades/manual.
 *
 * `quantity` accepts fractions — `trades.quantity` is NUMERIC(18,8) since
 * migration 010, so a 0.25-share fill is stored as 0.25 rather than rounded.
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

  /**
   * What the position-size calculator sized this trade against, captured at
   * entry rather than looked up later — account size drifts, and a trade sized
   * against $2,500 must keep reading as 1% of $2,500.
   *
   * `risk_amount` is the figure that turns an R-multiple back into dollars.
   * Note the asymmetry server-side: omitting `risk_percent` falls back to the
   * column default of 1.00, so only `risk_amount` reliably distinguishes a
   * sized trade from an unsized one.
   */
  risk_percent?: number | null;
  risk_amount?: number | null;

  /** Playbook entry this trade follows. */
  strategy_id?: string | null;
  /** Why the trade was taken, recorded at entry. */
  thesis?: string | null;
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

/**
 * Trader-level defaults (GET /api/settings) — one row, server-side.
 *
 * Current state only, not a history. What each trade actually risked lives on
 * the trade itself; this is what the calculator opens with.
 */
export interface AppSettings {
  /** Net liquidation value. Null means never set — not zero. */
  account_size: number | null;
  /** Percent, not fraction: 1.0 means 1%. */
  risk_percent: number;
  updated_at: string | null; // ISO 8601
}

/** Body for PUT /api/settings. Only keys present are applied. */
export interface AppSettingsPayload {
  /** Explicit null clears it; risk_percent may not be nulled. */
  account_size?: number | null;
  risk_percent?: number;
}

/**
 * One execution in the ledger (GET /api/trades) — the master list.
 *
 * Distinct from `Position`: a position is a *closed* round trip, so a buy
 * that has not been sold has no position row at all. This is every fill,
 * open or closed.
 */
export interface Trade {
  id: string;
  ticker: string;
  direction: TradeSide;
  quantity: number;
  actual_entry: number;
  exit_price: number | null;
  entry_date: string;
  style: string;
  source_tag: string | null;
  strategy_id: string | null;
  thesis: string | null;
  planned_entry: number | null;
  stop_loss: number | null;
  target: number | null;
  /** False means no counterpart fill yet � an open position. */
  is_matched: boolean;
  created_at: string | null;
}

/**
 * One trade idea, however many executions it took — GET /api/round-trips.
 *
 * The ledger used to list raw fills, which is why a single CRWD trade appeared
 * as four rows: the broker filled the entry with two orders and the exit with
 * two more. Those executions were always one round trip in the data; this is
 * the shape that says so.
 */
export interface RoundTrip {
  kind: 'closed' | 'open';
  key: string;
  position_id: string | null;
  /** The execution that owns the plan. A scale-in has several candidates. */
  plan_trade_id: string | null;

  symbol: string;
  direction: TradeSide;
  quantity: number;
  entry_price: number;
  exit_price: number | null;
  entry_time: string;
  exit_time: string | null;
  realized_pnl: number | null;
  execution_count: number;

  /** Computed server-side from entry/exit/stop, never stored. */
  r_multiple: number | null;
  planned_r_multiple: number | null;

  strategy_id: string | null;
  thesis: string | null;
  planned_entry: number | null;
  stop_loss: number | null;
  actual_stop_loss: number | null;
  target: number | null;
  risk_percent: number | null;
  risk_amount: number | null;
  conviction: number | null;
  emotional_state: string | null;

  review_status: string | null;
  trade_grade: string | null;
  notes: string | null;
  mistakes: string[];
  review_went_well: string | null;
  review_went_wrong: string | null;
  review_lessons: string | null;
  exit_reason: string | null;
  ideal_entry: number | null;
  ideal_stop: number | null;
  ideal_target: number | null;
  revised_entry: number | null;
  revised_stop: number | null;
  revised_target: number | null;
  disciplines: PositionDiscipline[];

  fills: PositionFill[];
}

/**
 * What DELETE /api/trades/{id} actually did.
 *
 * Deleting one fill can dissolve a whole round trip and discard its review,
 * because a position's size and P&L are derived from a specific set of
 * executions. Returned so the UI can say so rather than let the user find out
 * from a changed number later.
 */
export interface TradeDeleteResult {
  deleted_trade_id: string;
  ticker: string;
  positions_removed: number;
  positions_rebuilt: number;
  reviews_discarded: number;
}

/** Body for PATCH /api/trades/{id}. Only present keys are applied. */
export interface TradeAnnotationPayload {
  strategy_id?: string | null;
  thesis?: string | null;
  /**
   * The plan, carried by the round trip's opening execution — the only place a
   * still-open trade can hold one, since `positions` rows exist only once it
   * closes.
   */
  planned_entry?: number | null;
  stop_loss?: number | null;
  actual_stop_loss?: number | null;
  target?: number | null;
  risk_percent?: number | null;
  risk_amount?: number | null;
  /** 1–5, rated at entry — correlates against realised R. */
  conviction?: number | null;
  emotional_state?: string | null;
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

// ---------------------------------------------------------------------------
// Advanced analytics
// ---------------------------------------------------------------------------

export interface MistakeBreakdown {
  mistake: string;
  trade_count: number;
  total_r: number;
  avg_r: number;
  win_rate_pct: number;
}

/**
 * GET /api/analytics/advanced
 *
 * Several fields are deliberately `number | null`. Null means "not
 * computable from this sample" — no scoreable trades, no losses, no planned
 * entries — which is distinct from a real value of 0.
 */
export interface AdvancedMetrics {
  /** Trades with both an exit and a usable stop, so R could be computed. */
  scored_trades: number;
  /** Trades skipped: still open, or missing/invalid stop. */
  unscored_trades: number;
  total_r: number;
  avg_r: number | null;
  win_rate_pct: number;
  /** Null when there are no losing trades — unbounded ratio. */
  profit_factor_r: number | null;
  expectancy_r: number | null;
  /** Positive means worse fills than planned. Null when nothing was planned. */
  avg_slippage: number | null;
  slippage_sample: number;
  r_distribution: Record<string, number>;
  mistake_breakdown: MistakeBreakdown[];
  discipline_breakdown: DisciplineBreakdown[];
  strategy_breakdown: StrategyBreakdown[];
}

/**
 * Performance of one playbook entry, measured in R.
 *
 * Resolved through `trades.strategy_id`, so this stays joined to the strategy
 * playbook by id — renaming an entry there carries through here rather than
 * orphaning its history.
 */
export interface StrategyBreakdown {
  strategy: string;
  /** Every trade attributed to the setup, scoreable or not. */
  trade_count: number;
  /** How many could be scored in R — the rest have no stop recorded. */
  scored: number;
  unscored: number;
  total_r: number;
  avg_r: number | null;
  win_rate_pct: number | null;
  best_r: number | null;
  worst_r: number | null;
  net_pnl: number;
  first_traded: string | null;
  last_traded: string | null;
}

/** One side of a discipline split — trades that honoured a rule, or didn't. */
export interface DisciplineSideStats {
  trade_count: number;
  /** From realised P&L, so available on every closed trade. Null if none. */
  win_rate_pct: number | null;
  /** Only from trades carrying a stop. Null when none can be scored. */
  avg_r: number | null;
  /** How many trades actually stand behind `avg_r`. */
  r_sample: number;
}

/**
 * What one of the trader's own rules is measurably worth.
 *
 * `edge_*` is null when one side has no trades: a rule followed every time
 * has no counterfactual to compare against.
 */
export interface DisciplineBreakdown {
  discipline: string;
  followed: DisciplineSideStats;
  not_followed: DisciplineSideStats;
  edge_win_rate_pct: number | null;
  edge_r: number | null;
  sample: number;
}

// ---------------------------------------------------------------------------
// Dashboard view models
// ---------------------------------------------------------------------------

/**
 * View model for the KPI stat strip.
 *
 * camelCase because it is a presentation shape, not an API payload -- it is
 * mapped from `CoreStats` in the dashboard page.
 */
export interface KPIStats {
  netPnl: number;
  winRate: number;
  totalTrades: number;
  /** null when there are no losing trades - the ratio is unbounded. */
  profitFactor: number | null;
  avgRoi: number;
  pendingCount: number;
}

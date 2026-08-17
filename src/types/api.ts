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
  /**
   * How much history references this entry. Present on the list endpoint only
   * — a strategy just created has nothing pointing at it yet, and a zero there
   * would read as a count that was taken rather than one that was skipped.
   */
  usage?: StrategyUsage;
}

/**
 * Reference counts for one playbook entry.
 *
 * `trades` and `positions` are different grains of the same history — a round
 * trip is built from trades — so they are reported separately rather than
 * summed, which would count one trade twice.
 */
export interface StrategyUsage {
  trades: number;
  positions: number;
  plans: number;
  /**
   * Checklist rules scoped to this strategy (migration 032). NOT part of
   * what forces a reassignment target on delete — these cascade away with
   * the strategy automatically, so a never-traded strategy whose checklist
   * is already written can still be deleted outright.
   */
  checklist_items: number;
}

/**
 * What deleting a strategy moved before it removed anything.
 *
 * Deletion requires a reassignment target whenever anything references the
 * strategy. The FKs are ON DELETE SET NULL, so a bare delete would lose no
 * trade — but every trade tagged with it would fall into "Unassigned" in the
 * analytics breakdown with nothing left to say which setup it belonged to.
 */
export interface StrategyDeleteResult {
  deleted_id: string;
  deleted_name: string;
  /** Null when the strategy was unused and nothing needed moving. */
  reassigned_to_id: string | null;
  reassigned_to_name: string | null;
  trades_reassigned: number;
  positions_reassigned: number;
  plans_reassigned: number;
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
  /**
   * Null is a general rule, checked on every reviewed trade. Set, it scopes
   * the rule to one playbook entry (migration 032) — only relevant on a
   * trade tagged with that same strategy.
   */
  strategy_id: string | null;
  created_at: string | null;
}

export interface DisciplineCreatePayload {
  name: string;
  /** Omitted or null creates a general rule. */
  strategy_id?: string | null;
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
  /**
   * NET of commission since migration 020 — what actually reached the account.
   *
   * It was the price move alone until then, on every surface, which is why the
   * journal never tied out against a broker statement. `gross_pnl` is that old
   * figure, kept so the difference is inspectable rather than implied, and
   * `gross_pnl - commission === realized_pnl` holds exactly.
   */
  realized_pnl: number;
  gross_pnl: number | null;
  /** A cost: positive is paid, negative is a rebate IBKR passed through. */
  commission: number | null;

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
 * Submitting this flips `review_status` to 'reviewed' server-side, unless
 * `mark_reviewed: false` is sent alongside it.
 */
export interface PositionReviewPayload {
  /**
   * Completing the checklist is what empties the Trade Inbox queue, so this
   * defaults true. The Analytics drawer only ever sends `notes`/`mistakes`
   * and sets this false, so a note on an old trade doesn't silently clear it.
   */
  mark_reviewed?: boolean;
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
 * What kind of thing you would have to change to stop a Flex query failing.
 *
 * The distinction the sync used to lose. A statement IBKR has not compiled
 * yet, an expired token and a deleted query all arrive as "a query did not
 * return", and advising a retry is right for exactly one of them.
 */
export type FlexFailureCategory = 'wait' | 'query' | 'token' | 'request';

/**
 * One failed Flex query, with IBKR's own message and this app's reading of it.
 *
 * `message` is IBKR's wording, unmodified, so the interpretation beside it can
 * always be checked against the source.
 */
export interface FlexFailure {
  message: string;
  /** Null when the failure carried no IBKR code — a network fault, not a refusal. */
  code: string | null;
  label: string;
  category: FlexFailureCategory | string;
  /** Empty when the label already says everything useful. */
  guidance: string;
}

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
   * Fills IBKR reported THIS RUN with no execution time. Promoting one with a
   * fabricated "now" would corrupt FIFO match order and misplace it on the
   * heatmap, so it is left out of the ledger entirely rather than guessed at.
   * Can overlap with skipped_unpriced — a fill missing both counts in both.
   */
  skipped_undated?: number;
  /** Fills reported THIS RUN with no price. Same treatment, same reason. */
  skipped_unpriced?: number;
  /**
   * The true count of fills not imported THIS RUN — the union of the two
   * counters above, not their sum, so a fill missing both fields is not
   * double-counted.
   */
  skipped_unusable?: number;
  /**
   * How many fills, RIGHT NOW, can never be imported without help — queried
   * fresh every sync rather than counted from this run alone, because the
   * condition is standing, not an event. Stays nonzero on every subsequent
   * sync until IBKR resends the fill corrected, or it is added by hand via
   * the repair-fill modal.
   */
  stranded_fills?: number;
  /** The distinct tickers those fills belong to. */
  stranded_symbols?: string[];
  /**
   * Queries that did not return on this run. IBKR rate-limits report
   * generation per token and its cooldown outlasts a request, so a sync can
   * legitimately return part of the picture — this says which part is missing
   * rather than letting a partial sync look complete.
   */
  queries_failed: string[];
  /**
   * The same failures, read: which IBKR code, what it means, and whether
   * waiting can fix it. Prefer this over `queries_failed` + `rate_limited`
   * when present — those collapse every cause into "retry shortly", which is
   * actively wrong for an expired token or a deleted query.
   *
   * Optional so a frontend deploy landing before the API one still renders.
   */
  flex_failures?: FlexFailure[];
  /**
   * Broker fills re-sent that you had deliberately deleted, and ingest
   * skipped. A number that keeps climbing means the Flex query is still
   * returning something the journal does not want.
   */
  suppressed_skipped?: number;
  /**
   * True when at least one failed query was throttled rather than rejected.
   * Only throttling is worth retrying — a bad token fails identically forever.
   */
  rate_limited?: boolean;
  /**
   * Pre-trade plans this sync matched to the fills that finally arrived — the
   * moment the two halves of the journal meet.
   */
  plans_attached?: number;
  /**
   * True when the sync wrote refreshed broker figures onto existing ledger
   * rows.
   *
   * The one write that can move a displayed number while every fill counter
   * stays zero and `symbols_touched` stays empty — `broker_cost_basis` feeds
   * the open-exposure figures on `/api/round-trips`. Anything deciding whether
   * a sync changed something has to read this, or it will be right on every
   * run except the one where IBKR re-lots.
   *
   * Optional so a browser running ahead of the API still type-checks; absent
   * must be read as "assume it did", never as false.
   */
  broker_figures_refreshed?: boolean;
  /**
   * Round trips that existed before this sync and no longer survive
   * re-matching, because a fill arrived dated earlier than ones already stored
   * and re-partitioned the FIFO queue.
   *
   * Almost always zero. When it is not, net P&L and trade count have just
   * changed for a reason the imported-fill counts alone do not explain.
   */
  positions_removed?: number;
  /** How many of those carried a review. That part cannot be reconstructed. */
  reviews_discarded?: number;
  /**
   * Tickers this sync re-matched because an earlier run promoted their fills
   * and then died before building round trips from them — a container restart,
   * a redeploy, a dropped connection.
   *
   * Normally empty. When it is not, those fills had been sitting in the ledger
   * as exposure no statistic could see, and every figure derived from those
   * tickers has just moved.
   */
  symbols_recovered?: string[];
}

/** One day on the equity curve. Every calendar day gets one, trades or not. */
export interface EquityCurvePoint {
  date: string; // YYYY-MM-DD, in market time
  /** P&L closed on this day alone. Zero on a quiet day. */
  realized_pnl: number;
  /** Running total of closed P&L. This is what the line plots. */
  cumulative_pnl: number;
  /** High-water mark so far, floored at zero. */
  peak_pnl: number;
  /** Distance below the high-water mark. Always <= 0, so deeper is lower. */
  drawdown: number;
  /** Round trips closed on this day. */
  trades: number;
}

export interface EquityCurveSummary {
  start_date: string | null;
  end_date: string | null;
  net_pnl: number;
  peak_pnl: number;
  max_drawdown: number;
  current_drawdown: number;
  /** Days something actually closed. */
  trading_days: number;
  /** Days elapsed, including the quiet ones. */
  calendar_days: number;
  closed_trades: number;
  /**
   * True when closes older than `max_days` before the last one were dropped.
   *
   * The curve emits a point per calendar day, so a single corrupt execution
   * timestamp would otherwise stretch it across decades — one 1970 row beside
   * 2026 data measured 20,637 points in a single response. Surfaced rather
   * than applied silently: a curve that starts later than the data does is
   * indistinguishable from an account that began trading then.
   *
   * Optional so a frontend deploy landing before the API one still renders.
   */
  truncated?: boolean;
  /** The ceiling, in calendar days. 3650 (ten years). */
  max_days?: number;
}

/** Built-in dashboard windows. Resolved server-side — see `DashboardWindow`. */
export type TimeframePresetName = 'YTD' | '1Y' | 'ALL';

/** The pill selected in the toolbar: a built-in, or a saved preset's id. */
export type TimeframeSelection =
  | { kind: 'preset'; preset: TimeframePresetName }
  | { kind: 'custom'; id: string; start_date: string; end_date: string };

/**
 * What the timeframe toolbar needs in order to label itself.
 *
 * Kept to exactly the fields it renders so that any windowed payload can
 * satisfy it — the dashboard's fuller `DashboardWindow` does structurally,
 * and the advanced-metrics payload provides these and nothing more rather
 * than padding out curve-specific fields it has no answer for.
 */
export interface ToolbarWindow {
  start_date: string | null;
  end_date: string | null;
  closed_trades_in_window: number;
  closed_trades_total: number;
}

/**
 * The span a dashboard payload actually covers, echoed back by the API.
 *
 * Read rather than assumed: the client names a preset, the server decides what
 * it means. Keeping one definition of "YTD" is the point — two would drift.
 */
export interface DashboardWindow {
  /** The built-in that produced it, or null for an explicit date range. */
  preset: TimeframePresetName | null;
  /** Inclusive, market time. Null on either side means unbounded (ALL). */
  start_date: string | null;
  end_date: string | null;
  /** True when the corrupt-date clamp fired. See EquityCurveSummary. */
  truncated: boolean;
  max_days: number;
  closed_trades_in_window: number;
  closed_trades_total: number;
  /**
   * Round trips excluded for carrying no exit time. `positions.exit_time` is
   * NOT NULL, so this is expected to be zero — reported so that if it ever is
   * not, the shortfall is visible rather than showing up as figures that
   * quietly do not add up.
   */
  excluded_undated: number;
}

/**
 * A saved custom window (GET /api/settings/timeframes).
 *
 * The built-in YTD/1Y/ALL pills deliberately have no row: they are
 * definitions, not data. These live in the database rather than localStorage
 * so a filter you look at every day follows you between devices.
 */
export interface TimeframePreset {
  id: string;
  name: string;
  /** YYYY-MM-DD, market time. Inclusive on both ends. */
  start_date: string;
  end_date: string;
  created_at: string | null;
  updated_at: string | null;
}

/** Body for POST and PUT /api/settings/timeframes. A full replacement. */
export interface TimeframePresetPayload {
  name: string;
  start_date: string;
  end_date: string;
}

/**
 * Cumulative realised P&L over time — deliberately not called account equity.
 *
 * True equity needs a starting balance and every deposit and withdrawal, none
 * of which the broker feed carries. This is the sum of closed P&L, which the
 * data does support, and open positions are not in it.
 */
export interface EquityCurve {
  points: EquityCurvePoint[];
  summary: EquityCurveSummary;
}

/** Where a plan is in its life. Mirrors a CHECK constraint in the database. */
export type PlanStatus = 'OPEN' | 'ATTACHED' | 'CANCELLED';

/**
 * An unplanned opening leg a plan could be attached to by hand.
 *
 * Exists because a plan auto-attach declined is indistinguishable, from the
 * dock, from one whose fill simply has not arrived yet -- and the two want
 * opposite actions from the user. One is a click; the other is patience.
 */
export interface PlanCandidate {
  /** The fill to POST /api/trades/{trade_id}/attach-plan against. */
  trade_id: string;
  entry_date: string; // ISO 8601
  fill_count: number;
  quantity: number;
  avg_entry: number;
  /** Positive means the fill came before the plan was saved. */
  minutes_from_fill_to_plan: number | null;
}

/**
 * A trade you intend to take, before the broker knows anything about it.
 *
 * Deliberately carries no fill price. `trades` deduplicates on the broker's
 * execution id, so a hand-logged fill and IBKR's copy of the same execution
 * had no shared identifier and could never recognise each other — logging a
 * trade here and then syncing produced two rows for one real trade. A plan
 * lives in its own table so that collision cannot be expressed at all.
 */
export interface TradePlan {
  id: string;
  ticker: string;
  /** BUY or SELL. LONG/SHORT are accepted on write and normalised to these. */
  direction: TradeSide;
  quantity: number | null;
  planned_entry: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  /**
   * Computed by Postgres from the three prices above, never sent by a client.
   * Null when the triangle is incomplete or the entry equals the stop — which
   * is honest, where a zero would be a claim.
   */
  planned_r: number | null;
  risk_percent: number | null;
  risk_amount: number | null;
  strategy_id: string | null;
  thesis: string | null;
  status: PlanStatus;
  created_at: string | null; // ISO 8601
  updated_at: string | null;
  /**
   * Every fill this plan ended up covering. A list, not a single id: IBKR
   * splits one order into several executions, and the plan describes the
   * position rather than one slice of it.
   */
  attached_trade_ids: string[];
  /**
   * Whether a chart screenshot is attached. The storage key itself is never
   * sent — the bucket is private, so it would be useless here; the image is
   * fetched from GET /api/plans/{id}/chart instead.
   */
  has_chart: boolean;
  /** Stored size of that image, so the UI can report what charts cost. */
  chart_bytes: number | null;
  chart_uploaded_at: string | null; // ISO 8601
  /**
   * Pre-trade checklist answers (migration 033). Only rules actually
   * answered appear — same absence-means-unanswered convention as
   * `Position.disciplines`. Carried into the post-trade review as a starting
   * value only; never itself read by analytics.
   */
  disciplines: PositionDiscipline[];
  /**
   * Fills that match this plan but are not linked to it. Only ever populated
   * for an OPEN plan — an attached or cancelled one has nothing to offer.
   */
  candidates: PlanCandidate[];
}

/** POST /api/plans. Every price is optional — a ticker and a bias is enough. */
export interface TradePlanPayload {
  ticker: string;
  direction: TradeSide;
  quantity?: number | null;
  planned_entry?: number | null;
  stop_loss?: number | null;
  take_profit?: number | null;
  risk_percent?: number | null;
  risk_amount?: number | null;
  strategy_id?: string | null;
  thesis?: string | null;
  /** Keyed by discipline id. Omitting the field saves the plan unanswered. */
  disciplines?: Record<string, boolean>;
}

/** PATCH /api/plans/{id}. Only keys present are applied. */
export type TradePlanUpdatePayload = Partial<TradePlanPayload> & {
  status?: PlanStatus;
};

/** Result of linking a plan to a fill by hand. */
export interface PlanAttachResult {
  plan_id: string;
  ticker: string;
  /** Every fill the plan now covers, not only the one named in the request. */
  trade_ids: string[];
  /** Journal columns the plan filled in. Existing values are never replaced. */
  fields_copied: string[];
  status: PlanStatus;
}

/** Result of unlinking a plan from a fill. */
export interface PlanDetachResult {
  plan_id: string;
  ticker: string;
  trades_unlinked: number;
  /** Back to OPEN, so it can attach elsewhere. */
  status: PlanStatus;
}

/**
 * One tombstoned broker fill (GET /api/trades/suppressed).
 *
 * Everything but the id is nullable: tombstones written before the detail
 * columns existed have nothing to backfill from, because the fill they name
 * was deleted. Null means "not recorded", which is true.
 */
export interface SuppressedExecution {
  ibkr_exec_id: string;
  ticker: string | null;
  reason: string | null;
  direction: string | null;
  quantity: number | null;
  price: number | null;
  executed_at: string | null; // ISO 8601
  created_at: string | null;
}

/** Result of lifting a tombstone. */
export interface UnsuppressResult {
  ibkr_exec_id: string;
  ticker: string | null;
  /**
   * Always false, and named to be awkward to ignore. Lifting the tombstone
   * restores nothing by itself — the fill returns only when a sync next covers
   * its date.
   */
  restored_immediately: boolean;
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
  /**
   * What the fill cost to execute, as a COST — positive is paid, negative is a
   * rebate. IBKR reports the opposite sign on its statement, and the sync
   * negates it on the way in; a hand-typed repair fill is entered the way the
   * journal stores it.
   */
  commission?: number | null;
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

/** What set a sync off. Constrained in the database (migration 034). */
export type SyncTrigger = 'manual' | 'cron';

/**
 * How a sync ended. `partial` is its own outcome, not a flavour of success:
 * some Flex queries did not return, so the ledger is short of fills that exist
 * at the broker.
 */
export type SyncOutcome = 'success' | 'partial' | 'error';

/** One recorded sync run (GET /api/sync/runs/latest). */
export interface SyncRun {
  id: string;
  started_at: string; // ISO 8601
  finished_at: string | null;
  trigger: SyncTrigger | string;
  outcome: SyncOutcome | string;
  executions_parsed: number;
  trades_created: number;
  positions_matched: number;
  plans_attached: number;
  /** Only set when the run raised. */
  error: string | null;
}

/**
 * The server's view of syncing, which is the only one that can see a run this
 * browser did not perform.
 *
 * `last_success_at` is deliberately separate from `latest`: the newest run and
 * the newest run that WORKED are different questions, and only the second
 * answers "is the ledger current". A week of failing nightly runs has a very
 * recent `latest`, which is exactly how a broken schedule keeps looking busy.
 */
export interface SyncStatus {
  latest: SyncRun | null;
  last_success_at: string | null;
  /** Computed server-side, so the client is not trusting its own clock. */
  seconds_since_success: number | null;
}

/**
 * Outcome of the most recent broker sync attempt, as performed BY THIS TAB.
 *
 * Held in the React Query cache rather than component state so the header
 * badge and the sync button read the same fact. Local state in the button
 * could not be seen by the badge, which is why the badge used to claim
 * "CONNECTED" unconditionally — a status that was never checked.
 *
 * In-memory and per-session by nature, which is its limit: it cannot see a
 * scheduled run, or one performed in another tab. `SyncStatus` above is the
 * durable record; this remains the source for the detail toast, because it
 * carries the full `IngestResult` the moment the run returns.
 */
export interface LastSyncState {
  /** ISO timestamp of the attempt, successful or not. */
  at: string;
  outcome: 'success' | 'partial' | 'error';
  /** HTTP status when the server answered. Null when it never did. */
  status: number | null;
  /** Short human summary: "12 new", "up to date", or the error message. */
  summary: string;
  /**
   * The full ingest payload, so the summary toast can report every figure
   * without a second source of truth. Null when the request failed before the
   * server answered.
   */
  result: IngestResult | null;
  /**
   * Cleared when the user dismisses the toast. The badge keeps rendering from
   * the same entry — dismissing the detail must not erase the fact that a sync
   * happened.
   */
  acknowledged: boolean;
}

// ---------------------------------------------------------------------------
// Correcting the execution ledger
// ---------------------------------------------------------------------------

/**
 * Body for PATCH /api/trades/{id}/execution — the facts of a fill.
 *
 * Distinct from `TradeAnnotationPayload`, which records what you *thought* and
 * deliberately locks these fields. Changing a quantity re-runs FIFO and can
 * dissolve or create round trips; no annotation ever does that.
 */
export interface ExecutionUpdatePayload {
  direction?: TradeSide;
  quantity?: number;
  price?: number;
  /** Naive local string; the backend anchors it to America/New_York. */
  execution_time?: string | null;
}

export interface ExecutionUpdateResult {
  trade_id: string;
  ticker: string;
  direction: TradeSide;
  quantity: number;
  price: number;
  execution_time: string;
  /** Null while the fill is untouched since it arrived. */
  edited_at: string | null;
  /**
   * What IBKR originally reported, captured on the first edit and never
   * overwritten. Null on manual entries, which had no broker value.
   */
  broker_original: {
    direction?: string;
    quantity?: string;
    price?: string;
    execution_time?: string | null;
  } | null;
  positions_removed: number;
  positions_rebuilt: number;
  reviews_discarded: number;
}

/**
 * A round trip that shares an execution with the one being deleted.
 *
 * One fill can belong to two positions: an oversell that flips long to short
 * closes the long and opens the short with the same execution. Deleting either
 * round trip's fills therefore destroys the other one, so it gets named before
 * the user confirms rather than reported afterwards.
 */
export interface SharedRoundTrip {
  position_id: string;
  symbol: string;
  quantity: number;
  realized_pnl: number;
  entry_time: string;
  exit_time: string;
  /** Carries a grade, notes or a post-mortem — the part re-matching cannot rebuild. */
  has_review: boolean;
}

/** GET /api/positions/{id}/delete-impact — what the delete would take with it. */
export interface PositionDeleteImpact {
  position_id: string;
  ticker: string;
  executions_deleted: number;
  shared_round_trips: SharedRoundTrip[];
  reviews_at_risk: number;
}

/** Result of DELETE /api/positions/{id} — removes the executions underneath. */
export interface PositionDeleteResult {
  position_id: string;
  ticker: string;
  executions_deleted: number;
  positions_rebuilt: number;
  suppressed_from_future_syncs: number;
  /** Round trips removed BESIDES the one asked for. Zero on an ordinary delete. */
  positions_removed: number;
  reviews_discarded: number;
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
  /**
   * True only when the WHOLE execution has been folded into closed round
   * trips. False while any part of it is still open.
   *
   * The distinction is not pedantic: an oversell closes the long it was aimed
   * at and opens a short with what is left over, so one fill can be half
   * matched and half a live position. This used to report true for it, and the
   * short was invisible everywhere in the app.
   */
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
  /** NET of commission (migration 020). See Position.realized_pnl. */
  realized_pnl: number | null;
  gross_pnl: number | null;
  commission: number | null;
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

  /**
   * Set when the opening fill was matched to a pre-trade plan.
   *
   * The distinction is the point: `planned_entry` filled in by an attached
   * plan was committed to before the outcome was known, while the same column
   * typed into the journal afterwards is a recollection. Only one of those is
   * evidence about your process, so the UI labels them differently.
   */
  plan_id: string | null;
  /** When the plan was written — the proof that it predates the fill. */
  plan_created_at: string | null;
  /**
   * Whether the attached plan carries a chart screenshot. Sent so the ledger
   * renders the image only where there is one, rather than requesting it for
   * every planned trade and taking a 404 to find out.
   */
  plan_has_chart: boolean;
  /**
   * Per-share difference between the fill and the plan, signed so positive
   * always means BETTER than planned. That requires knowing the side: a short
   * filled above its planned entry got a better price, where a long paid up.
   */
  entry_slippage: number | null;
  /**
   * True when any fill here was typed in by hand to repair a gap in the broker
   * feed. A hand-typed price is an assertion, and should look like one next to
   * figures IBKR vouched for.
   */
  has_hand_added_fills: boolean;

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
  /**
   * True when a tombstone was written — this was a broker fill and the next
   * sync will not bring it back. False for manual entries, which no sync would
   * re-send anyway.
   */
  suppressed_from_future_syncs?: boolean;
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
  /**
   * Round trips that no longer survive re-matching. A repair fill is
   * backdated by definition, and inserting a fill before existing ones
   * re-partitions the FIFO queue — so previously closed round trips can
   * legitimately cease to exist.
   */
  positions_removed?: number;
  /** How many of those carried a review, which is the unrecoverable part. */
  reviews_discarded?: number;
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
  /**
   * Money realised in the window, summed from closing FILLS.
   *
   * Not the sum of the round trips below it, and deliberately so. A `positions`
   * row exists only once a ticker returns to flat, so summing round trips
   * omitted every dollar banked scaling out of a position still held — which
   * on this account was $75 of real losses, enough to report +66.20
   * year-to-date where the broker said −6.14. See `open_run_pnl`.
   */
  net_pnl: number;
  gross_pnl?: number;
  /**
   * ALL-IN cost: commission plus exchange, clearing and regulatory charges.
   *
   * Derived from IBKR's own `fifoPnlRealized` rather than from the commission
   * column, which carries only the commission. That is what makes
   * `gross_pnl - total_commission = net_pnl` reconcile to the IBKR statement
   * to the cent — verified at $0.00 variance across YTD, 90-day and Q4 windows.
   */
  total_commission?: number;
  /**
   * The IBKR commission alone, unmodified. The difference against
   * `total_commission` is what the broker charges beyond commission — about
   * 5.3¢ per closing fill.
   */
  ib_commission?: number;
  /**
   * Slices whose closing fill carried no broker figure, so their cost is the
   * commission alone and excludes the other charges. Zero on a broker-only
   * ledger; non-zero means the tie-out is approximate by that many slices.
   */
  unverified_legs?: number;
  /**
   * How much of `net_pnl` was banked out of positions that are still open.
   *
   * This is exactly the difference between `net_pnl` and the sum of the
   * completed round trips, so the two can be shown side by side without the
   * gap reading as a bug.
   */
  open_run_pnl?: number;
  /** Completed round trips only. A partial exit is not a finished idea. */
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
  /**
   * Optional so a frontend deploy that lands before the API one renders the
   * rest of the dashboard instead of crashing on a missing key.
   */
  equity_curve?: EquityCurve;
  /**
   * The span every figure above was computed over.
   *
   * One window governs the whole payload, not just the curve: a 1Y chart
   * beside an all-time win rate on one screen, with nothing saying they cover
   * different spans, is worse than either figure alone.
   *
   * Optional for the same deploy-ordering reason as `equity_curve`.
   */
  window?: DashboardWindow;
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
  compliance_buckets: ComplianceBucket[];
  strategy_breakdown: StrategyBreakdown[];
  /**
   * The span this payload covers, echoed back by the server.
   *
   * Narrower than `DashboardWindow` on purpose: `truncated` and `max_days`
   * describe the equity curve's clamp, and there is no curve on this payload.
   * Optional because an in-process caller may ask for the metrics with no
   * window at all.
   */
  window?: ToolbarWindow;
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

/**
 * Win rate and average R for one range of "how much of the answered
 * playbook did this trade follow" — the coarser question
 * `DisciplineBreakdown` can't answer, since that scores one rule at a time.
 *
 * `compliance` is one of a FIXED set of ranges ('100%', '80-99%', '50-79%',
 * '<50%'), always present in that order even when empty — matching
 * `r_distribution`'s own precedent of a complete shape a chart can render
 * without special-casing an empty bucket.
 */
export interface ComplianceBucket extends DisciplineSideStats {
  compliance: string;
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
  grossPnl: number;
  /** ALL-IN cost. grossPnl - totalCommission = netPnl, to the cent. */
  totalCommission: number;
  /** IBKR commission alone, for the split shown in the tooltip. */
  ibCommission: number;
  /** Slices with no broker figure behind them. Usually 0. */
  unverifiedLegs: number;
  /**
   * Of `netPnl`, the part banked scaling out of positions still open.
   *
   * Shown so the strip can explain why net P&L is not the sum of the round
   * trips underneath it. Before this existed that money was not merely
   * unexplained — it was not counted at all.
   */
  openRunPnl: number;
  winRate: number;
  totalTrades: number;
  /** null when there are no losing trades - the ratio is unbounded. */
  profitFactor: number | null;
  avgRoi: number;
  pendingCount: number;
}

/**
 * Data health audit — POST /api/audit.
 *
 * `clean` means every check passed. `attention` means something is worth
 * looking at but no displayed figure is known to be wrong (an unimportable
 * fill, a leg IBKR never gave us a figure for). `critical` means a number the
 * app is showing disagrees with the fills underneath it.
 */
export type AuditStatus = 'clean' | 'attention' | 'critical';

export interface AuditItem {
  ticker: string;
  /** STALE | MISSING | DRIFT | FILLS | BROKER | STRANDED */
  kind: string;
  detail: string;
}

export interface AuditCheck {
  key: string;
  label: string;
  status: AuditStatus;
  /** One sentence stating the finding, already phrased for display. */
  headline: string;
  /** Always complete, even when `items` is truncated. */
  counts: Record<string, number>;
  /** Capped server-side; the counts above are the authority. */
  items: AuditItem[];
}

export interface AuditResult {
  generated_at: string;
  duration_ms: number;
  tickers_checked: number;
  status: AuditStatus;
  checks: AuditCheck[];
}

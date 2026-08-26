/**
 * The long-term book. Deliberately a separate module from `types/api.ts`.
 *
 * No DOMAIN type here is shared with the trading journal, and that mirrors the
 * backend, where the investment endpoints are a self-contained section with
 * no reference in either direction. Keeping the types apart means a change to
 * one book cannot quietly alter the shape of the other.
 *
 * `FlexFailure` is the one deliberate exception, and it earns it by not being a
 * domain type: both books sync through the SAME IBKR Flex client and the same
 * classification table, so the backend serialises one `FlexFailureOut` for
 * both. Mirroring that single source with two independent interfaces would let
 * them drift from it -- which is the failure this module's separation exists to
 * prevent, pointed the other way.
 */

import type { FlexFailure } from './api';

/** The user's own taxonomy from the portfolio sheet. */
export type HoldingCategory = 'Growth' | 'Predictable' | 'ETF';

/** Which risk table a holding is priced against. */
export type ValuationRegion = 'US' | 'HK';

/**
 * ADJUSTMENT can appear in a GET of the ledger, but is never something the
 * create-transaction form offers -- it is written only by the
 * basis-correction endpoint, which computes its signed quantity/total_amount
 * itself (see migration 028 and TX_ADJUSTMENT in main.py).
 */
export type TransactionType = 'BUY' | 'SELL' | 'DIVIDEND' | 'TRANSFER' | 'ADJUSTMENT';

/** One stage of the twenty-year projection. */
export interface ValuationScenario {
  scenario: string;
  intrinsic_value: number;
  /** Data-derived. Years 6-10 and 11-20 are rules applied to it. */
  growth_1_5: number;
  growth_6_10: number;
  growth_11_20: number;
}

/**
 * What the DCF produced, or why it could not run.
 *
 * `available: false` is a real answer rather than an error — it means an
 * input is missing and the model has nothing to say. A zero intrinsic value
 * would render as "worth nothing", which is a claim about the business.
 */
export interface HoldingValuation {
  available: boolean;
  /** Only when `available` is false: which inputs are absent. */
  missing?: string[];
  discount_rate?: number;
  base?: ValuationScenario;
  conservative?: ValuationScenario;
  /** Simple mean of base and conservative, not a probability weighting. */
  average_intrinsic_value?: number;
  /**
   * (price / intrinsic value - 1) x 100. POSITIVE means the market is asking
   * MORE than the model says it is worth, so a negative figure is the
   * interesting one. Null when there is no price to compare against.
   */
  premium_pct?: number | null;
  /** Fields the user's override supplied, so the UI can mark them. */
  overridden_fields?: string[];
}

/** One row of `investment_valuation_inputs`, either variant. */
export interface ValuationInputRow {
  variant: 'auto' | 'override';
  base_flow: number | null;
  metric: string | null;
  shares_outstanding: number | null;
  total_debt: number | null;
  cash_and_st: number | null;
  beta: number | null;
  growth_1_5: number | null;
  discount_rate: number | null;
  region: ValuationRegion;
  source: string | null;
  updated_at: string | null;
}

/**
 * Never merged in storage, merged only on read.
 *
 * `auto` is replaced wholesale by each monthly refresh; `override` is never
 * touched by one. `merged` is what actually fed the model.
 */
export interface ValuationInputs {
  auto: ValuationInputRow | null;
  override: ValuationInputRow | null;
  merged: Record<string, number | string | null>;
}

export interface Holding {
  ticker: string;
  name: string | null;
  sector: string | null;
  category: HoldingCategory | null;
  holding_type: string | null;
  country: string | null;
  listed_currency: string;
  /** 1 USD in the listed currency. */
  exchange_rate: number;
  planned_allocation: number | null;
  /** False for a fund, which has no cash flows of its own to discount. */
  is_valuable: boolean;

  /** The manual override when set, otherwise the last auto-fetched quote --
   * whichever every other figure below (market value, unrealized P&L, DCF
   * premium) was actually computed from. */
  current_price: number | null;
  price_updated_at: string | null;
  /** Always the raw fetched quote, regardless of any override -- what
   * "reset to auto" reverts to. */
  auto_price: number | null;
  manual_price: number | null;
  manual_price_at: string | null;
  price_is_manual: boolean;
  /** The day's move in WHOLE PERCENT (-1.09 for -1.09%), as of
   * `day_change_updated_at` -- a snapshot from a price refresh, not a live
   * tape. Null means no refresh has fetched it yet, which is not 0. NOT
   * guaranteed to be from the SAME refresh as `price_updated_at` -- compare
   * the two timestamps (see `day_change_updated_at`) before presenting this
   * as today's move. */
  day_change_pct: number | null;
  /** When `day_change_pct` was last actually written. Equal to
   * `price_updated_at` when both came from the same refresh; older than it
   * when a later refresh fetched a new price but the provider omitted the
   * day change that time, leaving the prior figure in place -- see
   * `isDayChangeStale` in AllocationPanel.tsx. */
  day_change_updated_at: string | null;

  /** Derived from the transaction ledger on read, never stored. A correction
   * (see /basis-correction) is itself a ledger entry, not a second source
   * of truth -- these numbers already reflect any correction on record. */
  quantity: number;
  average_cost: number | null;
  cost_basis: number;
  /** Null when there is no price to multiply by. */
  market_value: number | null;
  unrealized_pnl: number | null;
  unrealized_pnl_pct: number | null;
  realized_pnl: number;
  dividends: number;
  transaction_count: number;
  first_acquired: string | null;

  portfolio_weight_pct: number | null;
  /** Null for a holding that is not valued at all, e.g. an ETF. */
  valuation: HoldingValuation | null;
  inputs: ValuationInputs;
}

export interface Portfolio {
  holdings: Holding[];
  total_market_value: number;
  total_cost_basis: number;
  total_unrealized_pnl: number;
  total_realized_pnl: number;
  total_dividends: number;
  as_of: string;
}

export interface InvestmentTransaction {
  id: string;
  ticker: string;
  transaction_type: TransactionType;
  quantity: number | null;
  price: number | null;
  /** Signed from the account's point of view, fees included. */
  total_amount: number;
  fees: number;
  transaction_date: string;
  listed_currency: string;
  exchange_rate: number;
  source: string;
  note: string | null;
  created_at: string | null;
}

/**
 * `total_amount` may be omitted and is derived from quantity, price and fees.
 * Send it explicitly only when a broker's cash figure is authoritative.
 */
export interface TransactionPayload {
  ticker: string;
  transaction_type: TransactionType;
  quantity?: number | null;
  price?: number | null;
  total_amount?: number | null;
  fees?: number;
  transaction_date: string;
  listed_currency?: string;
  exchange_rate?: number;
  note?: string | null;
}

/**
 * A correction to one ledger row. Type and ticker are deliberately absent --
 * changing either makes it a different transaction, which is a delete and a
 * create, not an edit; doing it in place would silently rewrite a derived
 * position rather than replacing the row that produced it.
 *
 * Only send the fields actually changing -- the backend applies `exclude_unset`,
 * so an omitted key leaves that column untouched.
 */
export interface TransactionUpdatePayload {
  quantity?: number | null;
  price?: number | null;
  total_amount?: number | null;
  fees?: number;
  transaction_date?: string;
  note?: string | null;
}

export interface HoldingPayload {
  ticker: string;
  name?: string | null;
  sector?: string | null;
  category?: HoldingCategory | null;
  holding_type?: string | null;
  country?: string | null;
  listed_currency?: string;
  exchange_rate?: number;
  planned_allocation?: number | null;
  is_valuable?: boolean;
  /** null clears the override and reverts to the auto-fetched quote. */
  manual_price?: number | null;
}

/**
 * Moves the derived position to exactly the quantity and/or average cost
 * given -- both are the FINAL state wanted, not a delta. Written as one
 * ADJUSTMENT transaction on the ledger, not a raw override; see migration
 * 028 for why average cost can't be a column the way price is.
 */
export interface BasisCorrectionPayload {
  quantity?: number | null;
  average_cost?: number | null;
  note?: string | null;
}

export interface BasisCorrectionResult {
  applied: boolean;
  detail?: string;
  quantity: number;
  average_cost: number | null;
  cost_basis: number;
}

/**
 * Every field optional, and null means "fall back to the fetched value for
 * this one" rather than "set this to nothing".
 */
export interface ValuationOverridePayload {
  base_flow?: number | null;
  metric?: string | null;
  shares_outstanding?: number | null;
  total_debt?: number | null;
  cash_and_st?: number | null;
  beta?: number | null;
  growth_1_5?: number | null;
  discount_rate?: number | null;
  region?: ValuationRegion | null;
}

/** Per-ticker outcome of a refresh, so a partial run can explain itself. */
export interface RefreshOutcome {
  ticker: string;
  status: 'refreshed' | 'skipped' | 'failed';
  detail?: string | null;
  growth_source?: string | null;
  /** False means a trailing CAGR stood in for a forward estimate. */
  growth_is_forward?: boolean | null;
  growth_clamped?: boolean | null;
}

export interface RefreshResult {
  refreshed: number;
  skipped: number;
  failed: number;
  outcomes: RefreshOutcome[];
}

export interface PriceRefreshResult {
  updated: number;
  failed: number;
  failures: { ticker: string; detail: string }[];
}

/**
 * Outcome of pulling fills from the long-term book's own IBKR account (a
 * separate query from the trading journal's). `queries_failed` holds one
 * message per Flex query that did not return; `flex_failures` says what each
 * one actually means -- not every refusal is rate-limiting, and only some of
 * them clear on their own.
 */
export interface SyncResult {
  fills_parsed: number;
  imported: number;
  duplicates: number;
  skipped: number;
  holdings_created: string[];
  queries_failed: string[];
  /** Optional so a frontend deploy landing before the API one still renders. */
  flex_failures?: FlexFailure[];
}

/** A manually-picked treemap color for one sector. Absent means "use the
 * built-in palette" -- this is only ever a sparse set of overrides. */
export interface SectorColor {
  sector: string;
  color: string;
  updated_at: string | null;
}

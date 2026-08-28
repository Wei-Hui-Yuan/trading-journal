/**
 * Position sizing and R-multiple targets.
 *
 * Ported from the "Trade Position Size Calculator" sheet, with two deliberate
 * departures:
 *
 *  1. The sheet is long-only. `Entry - Stop` goes negative on a short and the
 *     share count silently comes back negative, so direction is explicit here.
 *  2. The sheet computes one target at the chosen risk/reward ratio. This
 *     returns the whole R ladder (see `R_LADDER`), because deciding where to
 *     take profit is easier against the alternatives than in isolation.
 *
 * The `risk per share` denominator is deliberately identical to the one
 * services/analytics.py uses to score realised R. That is what makes a planned
 * 3R and an achieved 3R the same unit, and "what do I actually get when I plan
 * for 3R" a question the journal can answer.
 */

export type Side = 'BUY' | 'SELL';

export interface SizingInputs {
  side: Side;
  entry: number | null;
  stop: number | null;
  accountSize: number | null;
  riskPercent: number | null;
}

export interface RTarget {
  /** The multiple of risk this target represents — one of `R_LADDER`. */
  r: number;
  /** The price at which the trade is up this many R. */
  price: number;
  /** Profit in dollars at that price, for the sized position. Null if unsized. */
  profit: number | null;
}

export interface SizingResult {
  /** Risk per share — 1R. Always positive. */
  riskPerShare: number;
  /** Dollars at risk: account x risk%. Null when account size is unknown. */
  riskAmount: number | null;
  /** Exact fractional share count, or null when risk cannot be priced. */
  exactShares: number | null;
  /** Whole shares — what the quantity field is filled with. */
  wholeShares: number | null;
  /** Cost of the whole-share position at the entry price. */
  positionCost: number | null;
  /** Position cost as a share of the account, e.g. 2.14 for 214%. */
  accountFraction: number | null;
  targets: RTarget[];
}

/**
 * The R multiples the calculator surfaces.
 *
 * 5R is not a rung you climb to from 3R — it is a different kind of trade, and
 * the gap in the sequence is the point. A ladder of 1/2/3 quietly frames three
 * as the ceiling; including one target that only a runner reaches keeps the
 * question "am I cutting this at 2R out of habit?" on screen.
 */
export const R_LADDER = [1, 2, 3, 5] as const;

/**
 * Compute the ladder, or null if the inputs cannot support one.
 *
 * Returns null rather than a zeroed result when entry and stop are missing or
 * equal: a stop at the entry price is not "zero risk", it is an unanswerable
 * question, and dividing by it would report an infinite share count.
 */
export function computeSizing({
  side,
  entry,
  stop,
  accountSize,
  riskPercent,
}: SizingInputs): SizingResult | null {
  if (entry === null || stop === null) return null;
  if (!Number.isFinite(entry) || !Number.isFinite(stop)) return null;
  if (entry <= 0 || stop <= 0) return null;

  // Signed, so a stop on the wrong side of entry is caught rather than
  // absolute-valued into a plausible-looking number. A long stopping out above
  // its entry is a typo, and sizing it would hide that.
  const riskPerShare = side === 'BUY' ? entry - stop : stop - entry;
  if (riskPerShare <= 0) return null;

  const riskAmount =
    accountSize !== null &&
    riskPercent !== null &&
    Number.isFinite(accountSize) &&
    Number.isFinite(riskPercent) &&
    accountSize > 0
      ? (accountSize * riskPercent) / 100
      : null;

  const exactShares = riskAmount === null ? null : riskAmount / riskPerShare;
  // Floored, never rounded. Rounding 6.7 up to 7 buys more risk than the rule
  // allows, which is the one direction this calculator must never err in.
  const wholeShares = exactShares === null ? null : Math.floor(exactShares);

  const positionCost = wholeShares === null ? null : wholeShares * entry;
  const accountFraction =
    positionCost !== null && accountSize !== null && accountSize > 0
      ? positionCost / accountSize
      : null;

  const targets: RTarget[] = R_LADDER.map((r) => {
    // Targets move away from entry in the direction of the trade.
    const price =
      side === 'BUY' ? entry + r * riskPerShare : entry - r * riskPerShare;
    return {
      r,
      price,
      // Profit uses whole shares, matching what would actually be bought.
      profit: wholeShares === null ? null : wholeShares * r * riskPerShare,
    };
  });

  return {
    riskPerShare,
    riskAmount,
    exactShares,
    wholeShares,
    positionCost,
    accountFraction,
    targets,
  };
}

export interface PlannedRisk {
  /** Dollars at risk on the quantity actually chosen. */
  amount: number;
  /** That, as a percent of the account. Null when account size is unknown. */
  percent: number | null;
}

/**
 * What the quantity actually chosen puts at risk — as opposed to the budget
 * it was sized against.
 *
 * These are not the same number and the difference is not rounding noise.
 * `computeSizing` floors the share count, so a $25 budget against $10 of risk
 * per share buys two shares risking $20 — a fifth less than the budget
 * implies. Showing only the budget overstates what is on the line, and
 * showing only the share count leaves the reader to do the multiplication.
 *
 * Taking deliberate half size lands in the same place: the plan should record
 * half the risk, not the risk the calculator originally proposed.
 *
 * Shared rather than derived at each call site because the Plan modal sends
 * this figure to the server as `risk_amount` while both surfaces also render
 * it, and a display that disagrees with what was stored is worse than either
 * alone.
 */
export function computePlannedRisk({
  riskPerShare,
  shares,
  accountSize,
}: {
  riskPerShare: number;
  shares: number | null;
  accountSize: number | null;
}): PlannedRisk | null {
  if (shares === null || !Number.isFinite(shares) || shares <= 0) return null;
  if (!Number.isFinite(riskPerShare) || riskPerShare <= 0) return null;

  const amount = shares * riskPerShare;
  const percent =
    accountSize !== null && Number.isFinite(accountSize) && accountSize > 0
      ? (amount / accountSize) * 100
      : null;

  return { amount, percent };
}

export interface TakeProfitScore {
  /** Gain per share at that price. Negative when the target is backwards. */
  perShare: number;
  /** What that gain is worth in R. Same sign as `perShare`. */
  rMultiple: number;
  /** Dollar profit across `shares`, or null when the position is unsized. */
  profit: number | null;
  /** The share count `profit` was computed on, so the figure can say so. */
  shares: number | null;
  /**
   * True when the target sits on the losing side of the entry — a long taking
   * profit below where it bought. Surfaced rather than shown as a negative R,
   * because it is a typo rather than a strategy.
   */
  isBackwards: boolean;
}

/**
 * Score a take profit the user typed in, rather than one off the R ladder.
 *
 * The ladder answers "where is 2R?"; this answers the question people actually
 * arrive with, which is "I want out at 25 — what is that worth?". Without it
 * the only way to find out is to notice that 25 sits between the 1R and 2R
 * chips and interpolate, which is exactly the arithmetic the calculator exists
 * to remove.
 *
 * Direction-aware: on a short, profit is entry MINUS target, so the same
 * subtraction would report a winning target as a loss.
 */
export function scoreTakeProfit({
  side,
  entry,
  takeProfit,
  riskPerShare,
  shares,
}: {
  side: Side;
  entry: number;
  takeProfit: number;
  riskPerShare: number;
  shares: number | null;
}): TakeProfitScore | null {
  if (!Number.isFinite(entry) || !Number.isFinite(takeProfit)) return null;
  if (takeProfit <= 0) return null;
  // Guarded rather than assumed: this is the divisor, and computeSizing has
  // already refused to produce a result when it is not positive.
  if (!Number.isFinite(riskPerShare) || riskPerShare <= 0) return null;

  const perShare = side === 'BUY' ? takeProfit - entry : entry - takeProfit;

  return {
    perShare,
    rMultiple: perShare / riskPerShare,
    // Multiplied out per share rather than as rMultiple x riskAmount: the R is
    // displayed rounded to two decimals, and reusing the rounded figure drifts
    // the dollar amount away from what the position actually pays.
    profit: shares === null ? null : perShare * shares,
    shares,
    isBackwards: perShare <= 0,
  };
}

/**
 * Why sizing is unavailable, phrased for the user.
 *
 * Separate from computeSizing so the component renders a reason rather than an
 * empty panel — an inverted stop is worth being told about, not hidden.
 */
export function sizingHint({ side, entry, stop }: SizingInputs): string | null {
  if (entry === null || stop === null) return 'Enter Plan Entry and Plan Stop above.';
  if (entry <= 0 || stop <= 0) return 'Entry and stop must be greater than zero.';
  const riskPerShare = side === 'BUY' ? entry - stop : stop - entry;
  if (riskPerShare === 0) return 'Stop equals entry — there is no risk to size against.';
  if (riskPerShare < 0) {
    return side === 'BUY'
      ? 'For a long, the stop must sit below the entry.'
      : 'For a short, the stop must sit above the entry.';
  }
  return null;
}

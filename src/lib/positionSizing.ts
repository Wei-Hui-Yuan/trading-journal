/**
 * Position sizing and R-multiple targets.
 *
 * Ported from the "Trade Position Size Calculator" sheet, with two deliberate
 * departures:
 *
 *  1. The sheet is long-only. `Entry - Stop` goes negative on a short and the
 *     share count silently comes back negative, so direction is explicit here.
 *  2. The sheet computes one target at the chosen risk/reward ratio. This
 *     returns the whole 1R/2R/3R ladder, because deciding where to take profit
 *     is easier against the alternatives than in isolation.
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
  /** 1, 2, 3 — the multiple of risk this target represents. */
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

/** The R multiples the calculator surfaces. */
export const R_LADDER = [1, 2, 3] as const;

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

/**
 * Display formatting for money and percentages.
 *
 * Shared so the sign is applied in exactly one place. The KPI strip previously
 * prepended its own sign to an already-signed number and rendered `+-0.63%`,
 * and separately let `toLocaleString` default to three decimals, printing
 * `$-190.096` for a figure that is only ever accurate to the cent.
 */

/**
 * Money, to the cent, with the sign OUTSIDE the currency symbol.
 *
 * `-$190.10`, not `$-190.10`. Both are legible, but the first is the
 * convention every broker statement uses, and this app is read side by side
 * with one.
 *
 * Positive values carry an explicit `+` because these are P&L figures, where
 * the direction is the point. Zero gets no sign — it has no direction.
 */
export function formatMoney(value: number): string {
  const magnitude = Math.abs(value).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    // Both bounds are set. Without the maximum, toLocaleString defaults to 3
    // and a stored NUMERIC(_,4) leaks its extra digit into the UI.
    maximumFractionDigits: 2,
  });
  const sign = value > 0 ? '+' : value < 0 ? '-' : '';
  return `${sign}$${magnitude}`;
}

/**
 * Money with no explicit `+` on a gain — for figures that have a magnitude but
 * no direction.
 *
 * A position's cost, a risk budget, the profit at a 2R target: none of these
 * are P&L, and signing them the way `formatMoney` does would read as though
 * `+$2,000.00` of cost were somehow the good outcome. The sizing calculator
 * and the Plan modal each grew a private copy of this for exactly that reason;
 * this is that copy, made shared so the two cannot drift.
 *
 * A negative still prints its minus. Nothing the sizing panel renders through
 * this can be negative — a backwards take profit is caught and named before it
 * reaches a currency figure — but swallowing the sign would be the wrong way
 * to fail if one ever did.
 */
export function formatUnsignedMoney(value: number): string {
  return value.toLocaleString('en-US', { style: 'currency', currency: 'USD' });
}

/**
 * A price, at the precision the instrument warrants.
 *
 * Sub-dollar tickers need more than two decimals or every rung of the R ladder
 * rounds to the same number and the ladder reads as though it has no spacing.
 * Deliberately not `formatUnsignedMoney`: a price sits in a field the user
 * types into, so it is rendered bare, without a currency symbol or thousands
 * separators that would not survive being read back.
 */
export function formatPrice(value: number): string {
  return value < 1 ? value.toFixed(4) : value.toFixed(2);
}

/**
 * A percentage that signs itself.
 *
 * `toFixed` already emits the minus, so only the plus is ever added — adding
 * both is what produced `+-0.63%`.
 */
export function formatSignedPercent(value: number, digits = 2): string {
  return `${value > 0 ? '+' : ''}${value.toFixed(digits)}%`;
}

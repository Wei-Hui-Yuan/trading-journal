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
 * A percentage that signs itself.
 *
 * `toFixed` already emits the minus, so only the plus is ever added — adding
 * both is what produced `+-0.63%`.
 */
export function formatSignedPercent(value: number, digits = 2): string {
  return `${value > 0 ? '+' : ''}${value.toFixed(digits)}%`;
}

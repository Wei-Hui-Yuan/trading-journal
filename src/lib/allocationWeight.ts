/**
 * The "projected weight" preview shared by AddHoldingModal and
 * EditHoldingModal: what a target allocation WOULD weigh against the rest
 * of the book, before it is saved.
 *
 * `allocationNative` is typed by the trader in the holding's OWN currency
 * (see the `currency`/`exchangeRate` fields in either modal), but the base
 * it is weighed against (`baseUsd` -- the portfolio's total cost basis, or
 * that minus the holding's own, when editing) is already USD -- see
 * api/main.py's `get_portfolio`. Converting through `exchangeRate` before
 * comparing is what keeps this from mixing currencies, which was the
 * client-side twin of issue #5 of the calculation audit (the backend
 * summed native-currency figures across holdings as if they were all the
 * same currency; this component did the same thing comparing one native
 * figure against an already-USD total).
 */
export function projectedWeightPct(
  allocationNative: number,
  exchangeRate: number,
  baseUsd: number
): number | null {
  if (!Number.isFinite(allocationNative) || allocationNative <= 0) return null;
  const rate = exchangeRate || 1;
  const allocationUsd = allocationNative / rate;
  return (allocationUsd / (baseUsd + allocationUsd)) * 100;
}

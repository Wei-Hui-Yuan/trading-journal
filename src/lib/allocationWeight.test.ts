/**
 * projectedWeightPct: the "% of the book once funded" preview shared by
 * AddHoldingModal and EditHoldingModal.
 *
 * Before this existed, each modal compared an allocation typed in the
 * holding's OWN currency directly against `totalCostBasis`, which is the
 * portfolio's USD total (see api/main.py's get_portfolio) -- the same class
 * of currency-mixing bug issue #5 of the calculation audit found on the
 * backend, just on the client side of the "add/edit a target" flow.
 */

import { describe, expect, it } from 'vitest';

import { projectedWeightPct } from '@/lib/allocationWeight';

describe('projectedWeightPct', () => {
  it('returns null for a blank or zero allocation -- nothing to preview yet', () => {
    expect(projectedWeightPct(0, 1, 10000)).toBeNull();
    expect(projectedWeightPct(NaN, 1, 10000)).toBeNull();
  });

  it('returns null for a negative allocation', () => {
    expect(projectedWeightPct(-500, 1, 10000)).toBeNull();
  });

  it('weighs a USD allocation directly against a USD base (the common case today)', () => {
    // $1,000 target against a $9,000 book = 10% once funded.
    expect(projectedWeightPct(1000, 1, 9000)).toBeCloseTo(10, 6);
  });

  it('converts a foreign-currency allocation through its exchange rate before weighing it', () => {
    // 7,800 HKD at 7.80 HKD/USD is $1,000 -- same 10% answer as the USD
    // case above, proving the conversion happens before the comparison.
    expect(projectedWeightPct(7800, 7.8, 9000)).toBeCloseTo(10, 6);
  });

  it('a naive unconverted comparison would give a materially different (wrong) answer', () => {
    // The regression this guards against: comparing 7,800 directly against
    // a 9,000 USD base gives 46.4%, not 10% -- the bug issue #5 found.
    const wrong = (7800 / (9000 + 7800)) * 100;
    const right = projectedWeightPct(7800, 7.8, 9000);
    expect(right).not.toBeCloseTo(wrong, 1);
    expect(right).toBeCloseTo(10, 6);
  });

  it('falls back to a rate of 1 when the exchange rate is zero, blank, or NaN', () => {
    // Matches the `Number(exchangeRate) || 1` fallback both modals use --
    // an unparseable or cleared rate field must not divide by zero.
    expect(projectedWeightPct(1000, 0, 9000)).toBeCloseTo(10, 6);
    expect(projectedWeightPct(1000, NaN, 9000)).toBeCloseTo(10, 6);
  });

  it('weighs against a zero base as the entire book (a brand new portfolio)', () => {
    expect(projectedWeightPct(1000, 1, 0)).toBeCloseTo(100, 6);
  });
});

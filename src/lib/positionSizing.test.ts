/**
 * Position sizing: the arithmetic that decides how much real money goes on.
 *
 * This module has no second opinion anywhere. The backend scores realised R off
 * the same denominator, but nothing re-derives the share count, so a wrong
 * answer here is acted on rather than caught. The cases below are the ones the
 * module's own comments describe as having been got wrong before, or as the
 * direction it "must never err in".
 */

import { describe, expect, it } from 'vitest';

import {
  R_LADDER,
  breakevenWinRate,
  computePlannedRisk,
  blendedSizingResult,
  planScaledEntry,
  planScaledExit,
  computeSizing,
  scoreTakeProfit,
  sizingHint,
} from '@/lib/positionSizing';

const LONG = {
  side: 'BUY' as const,
  entry: 100,
  stop: 95,
  accountSize: 10_000,
  riskPercent: 1,
};

describe('computeSizing', () => {
  it('sizes a long off the distance to its stop', () => {
    const result = computeSizing(LONG)!;

    expect(result.riskPerShare).toBe(5);
    expect(result.riskAmount).toBe(100);
    expect(result.exactShares).toBe(20);
    expect(result.wholeShares).toBe(20);
    expect(result.positionCost).toBe(2000);
  });

  it('measures a short upwards, so it is not sized negative', () => {
    // The sheet this was ported from is long-only: entry minus stop goes
    // negative on a short and the share count comes back negative in silence.
    const result = computeSizing({ ...LONG, side: 'SELL', entry: 95, stop: 100 })!;

    expect(result.riskPerShare).toBe(5);
    expect(result.wholeShares).toBe(20);
  });

  it('floors the share count instead of rounding it', () => {
    // "Rounding 6.7 up to 7 buys more risk than the rule allows, which is the
    // one direction this calculator must never err in." 100/15 = 6.67.
    const result = computeSizing({ ...LONG, stop: 85 })!;

    expect(result.exactShares).toBeCloseTo(6.667, 3);
    expect(result.wholeShares).toBe(6);
  });

  it('never rounds up even a whisker below the next whole share', () => {
    // riskPerShare 1.0000001 gives 99.99999 shares. Rounding would authorise a
    // hundredth share more risk than the rule allows.
    const result = computeSizing({ ...LONG, entry: 100, stop: 100 - 1.0000001 })!;

    expect(result.exactShares!).toBeLessThan(100);
    expect(result.wholeShares).toBe(99);
  });

  it('refuses to size a stop sitting at the entry', () => {
    // Not zero risk -- an unanswerable question. Dividing by it would report an
    // infinite share count.
    expect(computeSizing({ ...LONG, stop: 100 })).toBeNull();
  });

  it.each([
    ['a long stopping out above its entry', { side: 'BUY' as const, stop: 105 }],
    ['a short stopping out below its entry', { side: 'SELL' as const, stop: 95 }],
  ])('refuses to size %s', (_label, override) => {
    // A typo. Absolute-valuing it would produce a plausible number and hide the
    // mistake instead of surfacing it.
    expect(computeSizing({ ...LONG, entry: 100, ...override })).toBeNull();
  });

  it.each([
    ['no entry', { entry: null }],
    ['no stop', { stop: null }],
    ['a zero entry', { entry: 0 }],
    ['a negative stop', { stop: -5 }],
    ['a NaN entry', { entry: Number.NaN }],
    ['an infinite stop', { stop: Number.POSITIVE_INFINITY }],
  ])('returns null for %s', (_label, override) => {
    expect(computeSizing({ ...LONG, ...override })).toBeNull();
  });

  it('still reports risk per share when the account size is unknown', () => {
    // The ladder is worth showing before settings are filled in: where 2R sits
    // does not depend on how much is being risked.
    const result = computeSizing({ ...LONG, accountSize: null })!;

    expect(result.riskPerShare).toBe(5);
    expect(result.riskAmount).toBeNull();
    expect(result.exactShares).toBeNull();
    expect(result.wholeShares).toBeNull();
    expect(result.positionCost).toBeNull();
    expect(result.accountFraction).toBeNull();
    // Prices stay knowable; only the money columns go blank.
    expect(result.targets.map((t) => t.price)).toEqual([105, 110, 115, 125]);
    expect(result.targets.every((t) => t.profit === null)).toBe(true);
  });

  it('puts targets above entry on a long and below it on a short', () => {
    const long = computeSizing(LONG)!;
    const short = computeSizing({ ...LONG, side: 'SELL', entry: 100, stop: 105 })!;

    expect(long.targets.map((t) => t.price)).toEqual([105, 110, 115, 125]);
    expect(short.targets.map((t) => t.price)).toEqual([95, 90, 85, 75]);
  });

  it('scales profit with the multiple, on the shares actually bought', () => {
    const { targets } = computeSizing({ ...LONG, stop: 85 })!;

    // Six whole shares, not the 6.67 the risk budget would have paid for.
    expect(targets.map((t) => t.profit)).toEqual([90, 180, 270, 450]);
    expect(targets.map((t) => t.r)).toEqual([...R_LADDER]);
  });

  it('reports a margined position as more than the account', () => {
    // risk_percent was widened from NUMERIC(4,2) in migration 015 for exactly
    // this case: a fraction over 1 is legal, not a bug to clamp.
    const result = computeSizing({ ...LONG, stop: 99.5, riskPercent: 5 })!;

    expect(result.accountFraction!).toBeGreaterThan(1);
  });
});

describe('computePlannedRisk', () => {
  it('reports what the floored share count actually risks, not the budget', () => {
    // The real case this was added for: $2,500 at 1% is a $25 budget, but
    // $10 of risk per share buys two shares risking $20. Showing only the
    // budget overstates the exposure by a fifth.
    const sizing = computeSizing({
      side: 'BUY',
      entry: 150,
      stop: 140,
      accountSize: 2_500,
      riskPercent: 1,
    })!;

    expect(sizing.riskAmount).toBe(25);
    expect(sizing.wholeShares).toBe(2);

    const planned = computePlannedRisk({
      riskPerShare: sizing.riskPerShare,
      shares: sizing.wholeShares,
      accountSize: 2_500,
    })!;

    expect(planned.amount).toBe(20);
    expect(planned.percent).toBeCloseTo(0.8, 10);
  });

  it('follows a deliberate half size rather than the suggestion', () => {
    const planned = computePlannedRisk({
      riskPerShare: 5,
      shares: 10,
      accountSize: 10_000,
    })!;

    expect(planned.amount).toBe(50);
    expect(planned.percent).toBeCloseTo(0.5, 10);
  });

  it('still prices the risk in dollars when the account size is unknown', () => {
    // The dollar figure is the more important half and does not depend on
    // knowing the balance.
    const planned = computePlannedRisk({
      riskPerShare: 5,
      shares: 10,
      accountSize: null,
    })!;

    expect(planned.amount).toBe(50);
    expect(planned.percent).toBeNull();
  });

  it.each([
    ['no quantity chosen', { shares: null }],
    ['a zero quantity', { shares: 0 }],
    ['a negative quantity', { shares: -5 }],
    ['a zero risk per share', { riskPerShare: 0 }],
    ['a negative risk per share', { riskPerShare: -5 }],
    ['a NaN quantity', { shares: Number.NaN }],
  ])('returns null for %s', (_label, override) => {
    // Null rather than zero: "nothing chosen yet" is not "risking nothing".
    expect(
      computePlannedRisk({
        riskPerShare: 5,
        shares: 10,
        accountSize: 10_000,
        ...override,
      })
    ).toBeNull();
  });

  it('does not divide by a zero account size', () => {
    const planned = computePlannedRisk({
      riskPerShare: 5,
      shares: 10,
      accountSize: 0,
    })!;

    expect(planned.amount).toBe(50);
    expect(planned.percent).toBeNull();
  });
});

describe('breakevenWinRate', () => {
  it.each([
    [1, 50],
    [2, 100 / 3],
    [3, 25],
    [5, 100 / 6],
  ])('puts %sR at %s%%', (r, expected) => {
    // The whole R ladder, since these are the four figures that will be read
    // off the screen most often.
    expect(breakevenWinRate(r)!).toBeCloseTo(expected, 10);
  });

  it('needs more than half the time below 1R', () => {
    // Risking one to make half means winning twice for every loss just to
    // stand still. Worth pinning: it is the direction people misjudge.
    expect(breakevenWinRate(0.5)!).toBeCloseTo(66.667, 3);
  });

  it('never reaches zero, however large the target', () => {
    // An asymptote, not a floor. A 99R target still has to be right sometimes.
    expect(breakevenWinRate(99)!).toBeGreaterThan(0);
    expect(breakevenWinRate(99)!).toBeCloseTo(1, 1);
  });

  it.each([
    ['a zero R', 0],
    ['a negative R', -2],
    ['a NaN R', Number.NaN],
    ['an infinite R', Number.POSITIVE_INFINITY],
  ])('returns null for %s', (_label, r) => {
    // A target at or behind entry has no breakeven win rate. Returning 100%
    // would be a wrong answer rather than a demanding one.
    expect(breakevenWinRate(r)).toBeNull();
  });
});

describe('scoreTakeProfit', () => {
  const base = { side: 'BUY' as const, entry: 100, riskPerShare: 5, shares: 20 };

  it('scores a target between two rungs of the ladder', () => {
    // The question people actually arrive with: out at 107, what is that worth?
    const score = scoreTakeProfit({ ...base, takeProfit: 107 })!;

    expect(score.perShare).toBe(7);
    expect(score.rMultiple).toBeCloseTo(1.4, 10);
    expect(score.profit).toBe(140);
    expect(score.isBackwards).toBe(false);
  });

  it('reads a short in the direction it profits', () => {
    // The same subtraction would report a winning short as a loss.
    const score = scoreTakeProfit({ ...base, side: 'SELL', takeProfit: 93 })!;

    expect(score.perShare).toBe(7);
    expect(score.rMultiple).toBeCloseTo(1.4, 10);
    expect(score.isBackwards).toBe(false);
  });

  it('flags a target on the losing side rather than reporting negative R', () => {
    // A long taking profit below where it bought is a typo, not a strategy.
    const score = scoreTakeProfit({ ...base, takeProfit: 95 })!;

    expect(score.isBackwards).toBe(true);
    expect(score.perShare).toBeLessThan(0);
  });

  it('treats a target exactly at entry as backwards', () => {
    // Zero gain is not a take profit. The implementation uses <= 0 rather than
    // < 0, which makes this boundary worth pinning.
    expect(scoreTakeProfit({ ...base, takeProfit: 100 })!.isBackwards).toBe(true);
  });

  it('computes profit per share rather than from the rounded R', () => {
    // "Reusing the rounded figure drifts the dollar amount away from what the
    // position actually pays." R here is 1.46666, displayed as 1.47.
    const score = scoreTakeProfit({ ...base, takeProfit: 107.3333 })!;

    expect(score.profit).toBeCloseTo(146.666, 3);
    // What the shortcut would have produced, and must not:
    expect(score.profit).not.toBeCloseTo(
      Number(score.rMultiple.toFixed(2)) * 5 * 20,
      3
    );
  });

  it('leaves profit unknown when the position is unsized', () => {
    const score = scoreTakeProfit({ ...base, shares: null, takeProfit: 107 })!;

    expect(score.profit).toBeNull();
    expect(score.shares).toBeNull();
    // The R is still knowable without a share count.
    expect(score.rMultiple).toBeCloseTo(1.4, 10);
  });

  it.each([
    ['a zero risk per share', { riskPerShare: 0 }],
    ['a negative risk per share', { riskPerShare: -5 }],
    ['a zero take profit', { takeProfit: 0 }],
    ['a NaN entry', { entry: Number.NaN }],
  ])('returns null for %s', (_label, override) => {
    expect(scoreTakeProfit({ ...base, takeProfit: 107, ...override })).toBeNull();
  });
});

describe('sizingHint', () => {
  it('says nothing when sizing is possible', () => {
    expect(sizingHint(LONG)).toBeNull();
  });

  it('names the side the stop belongs on', () => {
    // An inverted stop is worth being told about, not hidden behind an empty
    // panel -- which is the whole reason this is separate from computeSizing.
    expect(sizingHint({ ...LONG, stop: 105 })).toContain('below the entry');
    expect(sizingHint({ ...LONG, side: 'SELL', entry: 100, stop: 95 })).toContain(
      'above the entry'
    );
  });

  it('distinguishes a stop at the entry from one on the wrong side', () => {
    expect(sizingHint({ ...LONG, stop: 100 })).toContain('no risk to size against');
  });

  it('asks for the missing inputs first', () => {
    expect(sizingHint({ ...LONG, entry: null })).toContain('Enter Plan Entry');
    expect(sizingHint({ ...LONG, stop: null })).toContain('Enter Plan Entry');
  });

  it('rejects non-positive prices before judging direction', () => {
    expect(sizingHint({ ...LONG, entry: 0 })).toContain('greater than zero');
  });

  it('has a reason for every case computeSizing refuses', () => {
    // The pairing that matters. A null result with no hint renders as an empty
    // panel and no explanation, which is the failure this function exists to
    // prevent -- so the two must never disagree about what is unsizeable.
    const refused = [
      { ...LONG, entry: null },
      { ...LONG, stop: null },
      { ...LONG, entry: 0 },
      { ...LONG, stop: -1 },
      { ...LONG, stop: 100 },
      { ...LONG, stop: 105 },
      { ...LONG, side: 'SELL' as const, stop: 95 },
    ];

    for (const inputs of refused) {
      expect(computeSizing(inputs)).toBeNull();
      expect(sizingHint(inputs)).not.toBeNull();
    }
  });
});

describe('planScaledExit', () => {
  const base = { side: 'BUY' as const, entry: 100, riskPerShare: 5, shares: 100 };

  it('blends the R of every slice by its weight', () => {
    // Half off at 1R, a third at 2R, the rest at 4R.
    // (50x1 + 30x2 + 20x4) / 100 = 1.9
    const plan = planScaledExit({
      ...base,
      tranches: [
        { percent: 50, price: 105 },
        { percent: 30, price: 110 },
        { percent: 20, price: 120 },
      ],
    })!;

    expect(plan.legs.map((l) => l.rMultiple)).toEqual([1, 2, 4]);
    expect(plan.blendedR!).toBeCloseTo(1.9, 10);
    expect(plan.allocatedPercent).toBe(100);
    expect(plan.unallocatedPercent).toBe(0);
  });

  it('normalises the blend to what is allocated, not the whole position', () => {
    // 60% at 2R with a runner left over. The average EXIT is 2R; spreading
    // it over the whole position would report 1.2R, which is neither the
    // average exit nor what the position makes, and reads as though the
    // runner scored zero.
    const plan = planScaledExit({
      ...base,
      tranches: [{ percent: 60, price: 110 }],
    })!;

    expect(plan.blendedR!).toBeCloseTo(2, 10);
    expect(plan.allocatedPercent).toBe(60);
    expect(plan.unallocatedPercent).toBe(40);
  });

  it('sums dollars only over the shares actually sold', () => {
    // 50 shares at +$5, 50 at +$10.
    const plan = planScaledExit({
      ...base,
      tranches: [
        { percent: 50, price: 105 },
        { percent: 50, price: 110 },
      ],
    })!;

    expect(plan.legs.map((l) => l.shares)).toEqual([50, 50]);
    expect(plan.totalProfit).toBe(50 * 5 + 50 * 10);
    expect(plan.residualShares).toBe(0);
  });

  it('floors each leg rather than selling shares the position lacks', () => {
    // Five shares split three ways is 1.67 each. Rounding up sells six.
    const plan = planScaledExit({
      ...base,
      shares: 5,
      tranches: [
        { percent: 34, price: 105 },
        { percent: 33, price: 110 },
        { percent: 33, price: 115 },
      ],
    })!;

    expect(plan.legs.map((l) => l.shares)).toEqual([1, 1, 1]);
    // Two shares stranded by the flooring, which is the point of reporting it.
    expect(plan.residualShares).toBe(2);
  });

  it('flags a leg too small to fill a single share', () => {
    // A 10% slice of two shares is 0.2, which sells nothing. The blend still
    // counts it, so without this flag the plan reads as achievable.
    const plan = planScaledExit({
      ...base,
      shares: 2,
      tranches: [
        { percent: 90, price: 105 },
        { percent: 10, price: 120 },
      ],
    })!;

    expect(plan.hasEmptyLeg).toBe(true);
    expect(plan.legs[1].shares).toBe(0);
  });

  it('does not flag empty legs on a position that fills them all', () => {
    const plan = planScaledExit({
      ...base,
      tranches: [
        { percent: 50, price: 105 },
        { percent: 50, price: 110 },
      ],
    })!;

    expect(plan.hasEmptyLeg).toBe(false);
  });

  it('carries a backwards slice as backwards rather than dropping it', () => {
    // A long taking profit below entry is a typo, and silently discarding
    // the leg would make the blend look better than the plan actually is.
    const plan = planScaledExit({
      ...base,
      tranches: [
        { percent: 50, price: 110 },
        { percent: 50, price: 95 },
      ],
    })!;

    expect(plan.legs[1].isBackwards).toBe(true);
    expect(plan.legs[1].rMultiple).toBeLessThan(0);
    // (50x2 + 50x-1) / 100 = 0.5
    expect(plan.blendedR!).toBeCloseTo(0.5, 10);
  });

  it('reads a short in the direction it profits', () => {
    const plan = planScaledExit({
      ...base,
      side: 'SELL',
      tranches: [
        { percent: 50, price: 95 },
        { percent: 50, price: 90 },
      ],
    })!;

    expect(plan.legs.map((l) => l.rMultiple)).toEqual([1, 2]);
    expect(plan.blendedR!).toBeCloseTo(1.5, 10);
  });

  it('still blends when the position is unsized', () => {
    // Where the exits sit does not depend on how many shares are held.
    const plan = planScaledExit({
      ...base,
      shares: null,
      tranches: [
        { percent: 50, price: 105 },
        { percent: 50, price: 110 },
      ],
    })!;

    expect(plan.blendedR!).toBeCloseTo(1.5, 10);
    expect(plan.totalProfit).toBeNull();
    expect(plan.residualShares).toBeNull();
    expect(plan.legs.every((l) => l.shares === null)).toBe(true);
  });

  it.each([
    ['a zero percent', { percent: 0, price: 105 }],
    ['a negative percent', { percent: -10, price: 105 }],
    ['a zero price', { percent: 50, price: 0 }],
    ['a NaN price', { percent: 50, price: Number.NaN }],
  ])('skips %s rather than scoring it', (_label, bad) => {
    const plan = planScaledExit({
      ...base,
      tranches: [{ percent: 50, price: 110 }, bad],
    })!;

    expect(plan.legs).toHaveLength(1);
    expect(plan.allocatedPercent).toBe(50);
  });

  it('returns an empty plan, not null, when nothing is allocated', () => {
    // No legs is a plan not yet made, which is different from inputs that
    // cannot be scored at all.
    const plan = planScaledExit({ ...base, tranches: [] })!;

    expect(plan.legs).toEqual([]);
    expect(plan.blendedR).toBeNull();
    expect(plan.unallocatedPercent).toBe(100);
  });

  it('never reports a negative remainder when over-allocated', () => {
    // 130% is an input error the caller surfaces; a -30% remainder would
    // render as a nonsense figure beside it.
    const plan = planScaledExit({
      ...base,
      tranches: [
        { percent: 80, price: 105 },
        { percent: 50, price: 110 },
      ],
    })!;

    expect(plan.allocatedPercent).toBe(130);
    expect(plan.unallocatedPercent).toBe(0);
  });

  it.each([
    ['a zero risk per share', { riskPerShare: 0 }],
    ['a zero entry', { entry: 0 }],
    ['a NaN entry', { entry: Number.NaN }],
  ])('returns null for %s', (_label, override) => {
    expect(
      planScaledExit({
        ...base,
        ...override,
        tranches: [{ percent: 50, price: 110 }],
      })
    ).toBeNull();
  });
});

/**
 * A laddered entry, sized backwards from one risk budget.
 *
 * The case that motivated it: ARM, three entries at 236.80 / 226 / 216 against
 * one hard stop at 210, "total risking 1.5%". Each rung risks a different
 * amount per share -- 26.80, 16.00, 6.00 -- so this cannot be sized by
 * dividing a budget once, and the arithmetic that gets it wrong looks
 * plausible either way.
 *
 * The identity test below is the load-bearing one. A laddered plan is STORED
 * as a single blended entry and a single quantity, and that is only lossless
 * because `(blend - stop) x totalShares` is exactly the sum of the rungs'
 * risk. If that ever stops holding, every laddered plan in the journal starts
 * misreporting its own R.
 */

const LADDER = {
  side: 'BUY' as const,
  stop: 90,
  accountSize: 10_000,
  riskPercent: 1,
  tranches: [
    { percent: 50, price: 100 },
    { percent: 50, price: 95 },
  ],
};

describe('planScaledEntry', () => {
  it('sizes every rung off its own distance to the shared stop', () => {
    const plan = planScaledEntry(LADDER)!;

    // 10 and 5 a share -- the whole reason a ladder is not one division.
    expect(plan.legs.map((l) => l.riskPerShare)).toEqual([10, 5]);

    // riskPerUnit = 0.5(10) + 0.5(5) = 7.5, so floor(100/7.5) = 13 notional,
    // floored again to 6 a rung.
    expect(plan.legs.map((l) => l.shares)).toEqual([6, 6]);
    expect(plan.totalShares).toBe(12);
    expect(plan.legs.map((l) => l.riskAmount)).toEqual([60, 30]);
    expect(plan.legs.map((l) => l.cost)).toEqual([600, 570]);
  });

  it('blends to one entry whose 1R reproduces the ladder exactly', () => {
    const plan = planScaledEntry(LADDER)!;

    expect(plan.blend).toEqual({ entry: 97.5, riskPerShare: 7.5 });
    // The identity the whole storage decision rests on.
    expect(plan.blend!.riskPerShare * plan.totalShares!).toBeCloseTo(
      plan.totalRisk!,
      10
    );
  });

  it('lands under the budget rather than over it, and says by how much', () => {
    const plan = planScaledEntry(LADDER)!;

    // Flooring twice costs $10 of a $100 budget. Reported, not implied.
    expect(plan.riskBudget).toBe(100);
    expect(plan.totalRisk).toBe(90);
    expect(plan.totalRisk!).toBeLessThan(plan.riskBudget!);
    expect(plan.totalRiskPercent).toBeCloseTo(0.9, 10);
    expect(plan.totalCost).toBe(1170);
    expect(plan.accountFraction).toBeCloseTo(0.117, 10);
    expect(plan.allocatedPercent).toBe(100);
  });

  it('holds the ARM ladder that motivated it to its stated 1.5%', () => {
    const plan = planScaledEntry({
      side: 'BUY',
      stop: 210,
      accountSize: 100_000,
      riskPercent: 1.5,
      tranches: [
        { percent: 50, price: 236.8 },
        { percent: 30, price: 226 },
        { percent: 20, price: 216 },
      ],
    })!;

    // toBeCloseTo per rung: 236.8 - 210 lands on 26.80000000000001 in
    // binary floating point. Real noise, and the display layer rounds it
    // away, but an exact assertion here would be asserting the noise.
    expect(plan.legs[0].riskPerShare).toBeCloseTo(26.8, 10);
    expect(plan.legs.map((l) => l.riskPerShare).slice(1)).toEqual([16, 6]);
    expect(plan.legs.map((l) => l.shares)).toEqual([38, 23, 15]);
    expect(plan.totalRisk).toBeCloseTo(1476.4, 6);
    // Never over the 1.5% that was asked for.
    expect(plan.totalRiskPercent!).toBeLessThanOrEqual(1.5);
    expect(plan.blend!.entry).toBeCloseTo(229.4263, 4);
    expect(plan.blend!.riskPerShare * plan.totalShares!).toBeCloseTo(
      plan.totalRisk!,
      6
    );
  });

  it('mirrors onto a short, where the rungs sit below the stop', () => {
    const plan = planScaledEntry({
      ...LADDER,
      side: 'SELL',
      stop: 110,
      tranches: [
        { percent: 50, price: 100 },
        { percent: 50, price: 105 },
      ],
    })!;

    expect(plan.legs.map((l) => l.riskPerShare)).toEqual([10, 5]);
    expect(plan.blend).toEqual({ entry: 102.5, riskPerShare: 7.5 });
    expect(plan.totalRisk).toBe(90);
  });

  it('refuses to size when a rung is on the wrong side of the stop', () => {
    const plan = planScaledEntry({
      ...LADDER,
      tranches: [
        { percent: 50, price: 100 },
        // A long buying at 85 with the stop at 90: already stopped out.
        { percent: 50, price: 85 },
      ],
    })!;

    expect(plan.hasBackwardsLeg).toBe(true);
    expect(plan.legs.map((l) => l.isBackwards)).toEqual([false, true]);
    // Nothing sized -- a negative risk per share would have SUBTRACTED from
    // the ladder's cost and bought more shares than the budget allows.
    expect(plan.totalShares).toBeNull();
    expect(plan.totalRisk).toBeNull();
    expect(plan.totalRiskPercent).toBeNull();
    expect(plan.totalCost).toBeNull();
    expect(plan.accountFraction).toBeNull();
    expect(plan.legs.map((l) => l.shares)).toEqual([null, null]);
    expect(plan.legs.map((l) => l.riskAmount)).toEqual([null, null]);
    expect(plan.legs.map((l) => l.cost)).toEqual([null, null]);
    // The rungs still render, so the bad one can be found and fixed.
    expect(plan.legs).toHaveLength(2);
  });

  it('is empty rather than an error with no rungs', () => {
    const plan = planScaledEntry({ ...LADDER, tranches: [] })!;

    expect(plan.legs).toEqual([]);
    expect(plan.blend).toBeNull();
    expect(plan.totalShares).toBeNull();
    expect(plan.allocatedPercent).toBe(0);
    expect(plan.hasEmptyLeg).toBe(false);
    expect(plan.hasBackwardsLeg).toBe(false);
  });

  it('drops rows that cannot be read rather than sizing them as zero', () => {
    const plan = planScaledEntry({
      ...LADDER,
      tranches: [
        { percent: 50, price: 100 },
        { percent: Number.POSITIVE_INFINITY, price: 99 },
        { percent: 0, price: 98 },
        { percent: 25, price: Number.NaN },
        { percent: 25, price: 0 },
      ],
    })!;

    // Only the readable rung survives; a half-typed row is not a rung.
    expect(plan.legs).toHaveLength(1);
    expect(plan.legs[0].price).toBe(100);
    expect(plan.allocatedPercent).toBe(50);
  });

  it('reports a blend and no size when there is no account to size against', () => {
    for (const accountSize of [null, Number.POSITIVE_INFINITY, 0]) {
      const plan = planScaledEntry({ ...LADDER, accountSize })!;

      expect(plan.riskBudget).toBeNull();
      expect(plan.totalShares).toBeNull();
      expect(plan.accountFraction).toBeNull();
      // Percent-weighted, because there are no share counts to weight by.
      expect(plan.blend).toEqual({ entry: 97.5, riskPerShare: 7.5 });
    }
  });

  it('reports a blend and no size when there is no risk percent', () => {
    for (const riskPercent of [null, Number.NaN]) {
      const plan = planScaledEntry({ ...LADDER, riskPercent })!;

      expect(plan.riskBudget).toBeNull();
      expect(plan.totalShares).toBeNull();
      expect(plan.blend!.entry).toBe(97.5);
    }
  });

  it('names a rung too small to buy a whole share', () => {
    const plan = planScaledEntry({
      ...LADDER,
      tranches: [
        { percent: 99, price: 100 },
        { percent: 1, price: 95 },
      ],
    })!;

    expect(plan.legs.map((l) => l.shares)).toEqual([9, 0]);
    expect(plan.totalShares).toBe(9);
    expect(plan.hasEmptyLeg).toBe(true);
  });

  it('still prices a ladder the budget cannot afford one share of', () => {
    const plan = planScaledEntry({ ...LADDER, riskPercent: 0.01 })!;

    // $1 of budget against $7.50 a unit buys nothing at all.
    expect(plan.totalShares).toBe(0);
    expect(plan.totalRisk).toBe(0);
    expect(plan.totalCost).toBe(0);
    expect(plan.accountFraction).toBe(0);
    expect(plan.hasEmptyLeg).toBe(true);
    // Falls back to the percent-weighted average rather than dividing by
    // zero shares and reporting NaN as a price.
    expect(plan.blend).toEqual({ entry: 97.5, riskPerShare: 7.5 });
  });

  it('carries an over-allocated ladder through for the caller to flag', () => {
    const plan = planScaledEntry({
      ...LADDER,
      tranches: [
        { percent: 70, price: 100 },
        { percent: 70, price: 95 },
      ],
    })!;

    expect(plan.allocatedPercent).toBe(140);
  });

  it('has nothing to size against without a usable stop', () => {
    for (const stop of [null, Number.POSITIVE_INFINITY, 0, -5]) {
      expect(planScaledEntry({ ...LADDER, stop })).toBeNull();
    }
  });
});

describe('blendedSizingResult', () => {
  it('shapes the ladder as the one sized-position type the panel renders', () => {
    const plan = planScaledEntry(LADDER)!;
    const result = blendedSizingResult(plan, 'BUY')!;

    expect(result.riskPerShare).toBe(7.5);
    expect(result.riskAmount).toBe(100);
    expect(result.wholeShares).toBe(12);
    expect(result.positionCost).toBe(1170);
    expect(result.accountFraction).toBeCloseTo(0.117, 10);
    // No single fractional count exists for a doubly-floored ladder, so none
    // is claimed.
    expect(result.exactShares).toBeNull();
  });

  it('puts the R ladder above the blend on a long', () => {
    const result = blendedSizingResult(planScaledEntry(LADDER)!, 'BUY')!;

    expect(result.targets.map((t) => t.r)).toEqual([...R_LADDER]);
    expect(result.targets.map((t) => t.price)).toEqual([105, 112.5, 120, 135]);
    // 12 shares x 7.5 x R.
    expect(result.targets.map((t) => t.profit)).toEqual([90, 180, 270, 450]);
  });

  it('puts it below the blend on a short', () => {
    const plan = planScaledEntry({
      ...LADDER,
      side: 'SELL',
      stop: 110,
      tranches: [
        { percent: 50, price: 100 },
        { percent: 50, price: 105 },
      ],
    })!;
    const result = blendedSizingResult(plan, 'SELL')!;

    expect(result.targets.map((t) => t.price)).toEqual([95, 87.5, 80, 65]);
  });

  it('prices targets with no profit figure when the ladder is unsized', () => {
    const plan = planScaledEntry({ ...LADDER, accountSize: null })!;
    const result = blendedSizingResult(plan, 'BUY')!;

    expect(result.wholeShares).toBeNull();
    expect(result.targets.map((t) => t.price)).toEqual([105, 112.5, 120, 135]);
    expect(result.targets.map((t) => t.profit)).toEqual([null, null, null, null]);
  });

  it('declines an empty ladder', () => {
    const plan = planScaledEntry({ ...LADDER, tranches: [] })!;

    expect(blendedSizingResult(plan, 'BUY')).toBeNull();
  });

  it('declines a ladder whose blend sits on the wrong side of the stop', () => {
    // Every rung backwards, so the blend is too -- there is no positive 1R to
    // measure an R ladder against.
    const plan = planScaledEntry({
      ...LADDER,
      tranches: [
        { percent: 50, price: 85 },
        { percent: 50, price: 80 },
      ],
    })!;

    expect(plan.blend!.riskPerShare).toBeLessThan(0);
    expect(blendedSizingResult(plan, 'BUY')).toBeNull();
  });
});

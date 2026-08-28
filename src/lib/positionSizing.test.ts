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
  computePlannedRisk,
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

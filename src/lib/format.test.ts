/**
 * Display formatting. Every case here is a bug this module was written to fix.
 *
 * The module's own header names two of them: the KPI strip prepended its own
 * sign to an already-signed number and rendered `+-0.63%`, and separately let
 * toLocaleString default to three decimals, printing `$-190.096` for a figure
 * only ever accurate to the cent. Both are the kind of thing that is obvious
 * on screen and invisible in a diff.
 */

import { describe, expect, it } from 'vitest';

import {
  formatDuration,
  formatMoney,
  formatPrice,
  formatSignedPercent,
  formatUnsignedMoney,
} from '@/lib/format';

describe('formatMoney', () => {
  it('puts the sign outside the currency symbol', () => {
    // Every broker statement reads this way, and this app is read side by side
    // with one.
    expect(formatMoney(-190.1)).toBe('-$190.10');
    expect(formatMoney(190.1)).toBe('+$190.10');
  });

  it('never prints a third decimal', () => {
    // The bug verbatim: `$-190.096`. A NUMERIC(_,4) column leaking its extra
    // digit into a figure that is only accurate to the cent.
    expect(formatMoney(-190.096)).toBe('-$190.10');
    expect(formatMoney(1.005)).toBe('+$1.01');
  });

  it('always prints two decimals, even for a round figure', () => {
    expect(formatMoney(1000)).toBe('+$1,000.00');
    expect(formatMoney(-5)).toBe('-$5.00');
  });

  it('gives zero no sign, because it has no direction', () => {
    expect(formatMoney(0)).toBe('$0.00');
  });

  it('treats negative zero as zero rather than as a loss', () => {
    // -0 arises from summing a column that cancels out. `value < 0` is false
    // for -0, so this is already correct -- pinned so it stays that way.
    expect(formatMoney(-0)).toBe('$0.00');
  });

  it('groups thousands', () => {
    expect(formatMoney(1234567.89)).toBe('+$1,234,567.89');
  });

  it('rounds a sub-cent figure to the nearest cent rather than truncating', () => {
    expect(formatMoney(0.004)).toBe('+$0.00');
    expect(formatMoney(0.006)).toBe('+$0.01');
  });
});

describe('formatUnsignedMoney', () => {
  it('does not sign a gain, because these figures have no direction', () => {
    // The whole reason this is separate from formatMoney. A position's cost is
    // not a profit, and `+$2,000.00` would read as though it were.
    expect(formatUnsignedMoney(2000)).toBe('$2,000.00');
    expect(formatUnsignedMoney(0)).toBe('$0.00');
  });

  it('still shows a minus, rather than swallowing it', () => {
    // Nothing the sizing panel renders through this can be negative, but
    // hiding the sign would be the wrong way to fail if one ever did.
    expect(formatUnsignedMoney(-5)).toBe('-$5.00');
  });

  it('always prints two decimals, and groups thousands', () => {
    expect(formatUnsignedMoney(1234567.8)).toBe('$1,234,567.80');
  });

  it('rounds to the cent rather than leaking a stored fourth decimal', () => {
    expect(formatUnsignedMoney(190.096)).toBe('$190.10');
  });
});

describe('formatPrice', () => {
  it('gives a sub-dollar price four decimals', () => {
    // At two decimals every rung of the R ladder on a penny stock rounds to
    // the same number and the ladder reads as though it has no spacing.
    expect(formatPrice(0.1234)).toBe('0.1234');
    expect(formatPrice(0.9999)).toBe('0.9999');
  });

  it('gives a dollar-and-up price two', () => {
    expect(formatPrice(150)).toBe('150.00');
    expect(formatPrice(1)).toBe('1.00');
  });

  it('switches at exactly one dollar, not below it', () => {
    // `< 1` is the boundary in the implementation; pinned so a later tidy-up
    // to `<=` does not silently change every whole-dollar price on screen.
    expect(formatPrice(0.999999)).toBe('1.0000');
    expect(formatPrice(1.0)).toBe('1.00');
  });

  it('renders bare, with no currency symbol or separators', () => {
    // It is written back into a number field the user types into. A `$` or a
    // comma would not survive being read back.
    expect(formatPrice(1234.5)).toBe('1234.50');
  });
});

describe('formatSignedPercent', () => {
  it('does not double the sign on a negative', () => {
    // The bug verbatim: `+-0.63%`. toFixed already emits the minus, so only the
    // plus is ever added.
    expect(formatSignedPercent(-0.63)).toBe('-0.63%');
    expect(formatSignedPercent(-0.63)).not.toContain('+-');
  });

  it('adds an explicit plus to a gain', () => {
    expect(formatSignedPercent(0.63)).toBe('+0.63%');
  });

  it('gives zero no sign', () => {
    expect(formatSignedPercent(0)).toBe('0.00%');
  });

  it('honours a requested precision', () => {
    expect(formatSignedPercent(12.3456, 0)).toBe('+12%');
    expect(formatSignedPercent(12.3456, 1)).toBe('+12.3%');
    expect(formatSignedPercent(12.3456, 3)).toBe('+12.346%');
  });

  it('defaults to two decimals', () => {
    expect(formatSignedPercent(12.3456)).toBe('+12.35%');
  });

  it('does not sign a value that rounds to zero from below', () => {
    // -0.001 at two decimals is "-0.00%": toFixed keeps the minus even though
    // the displayed magnitude is zero. Pinned as the current behaviour rather
    // than asserted as ideal -- the alternative is claiming a loss was a gain.
    expect(formatSignedPercent(-0.001)).toBe('-0.00%');
  });
});

describe('formatDuration', () => {
  it('renders sub-hour figures as rounded minutes', () => {
    expect(formatDuration(0.5)).toBe('30m');
    // Rounds rather than truncates -- 59.4 minutes reads as "an hour" more
    // honestly than "59m" would, and truncating loses that.
    expect(formatDuration(0.99)).toBe('59m');
  });

  it('renders zero as zero minutes rather than nothing', () => {
    expect(formatDuration(0)).toBe('0m');
  });

  it('renders one hour up to two days as hours, one decimal', () => {
    expect(formatDuration(1)).toBe('1.0h');
    expect(formatDuration(6.25)).toBe('6.3h');
    expect(formatDuration(47.9)).toBe('47.9h');
  });

  it('renders two days and beyond as days, one decimal', () => {
    expect(formatDuration(48)).toBe('2.0d');
    expect(formatDuration(72)).toBe('3.0d');
    expect(formatDuration(240)).toBe('10.0d');
  });
});

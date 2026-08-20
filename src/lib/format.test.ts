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

import { formatMoney, formatSignedPercent } from '@/lib/format';

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

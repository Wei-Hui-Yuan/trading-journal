/**
 * PositionSizingPanel: what the trader actually reads before committing money.
 *
 * Every case is driven through the REAL `computeSizing` / `scoreTakeProfit`
 * rather than a hand-built `SizingResult` literal. A fabricated shape can
 * agree with the component and disagree with the library, which is precisely
 * the class of bug an extraction like this one can introduce — so the inputs
 * here are prices, and the library decides what they mean.
 *
 * This panel had no test of its own before it was extracted: the Plan modal's
 * suite covers its form and submit payload, never the sizing output it
 * rendered. Everything below is coverage that did not previously exist.
 */

import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { PositionSizingPanel } from '@/components/PositionSizingPanel';
import {
  computeSizing,
  scoreTakeProfit,
  sizingHint,
  type SizingInputs,
} from '@/lib/positionSizing';

afterEach(cleanup);

const LONG: SizingInputs = {
  side: 'BUY',
  entry: 100,
  stop: 95,
  accountSize: 10_000,
  riskPercent: 1,
};

/**
 * Render the panel for a set of prices, wiring it the way a host does.
 *
 * `takeProfit` is the number the host has in its own field; the score is
 * derived from it here for the same reason the hosts derive it — so the chip
 * row and the scorecard can never be quoting different arithmetic.
 */
function renderPanel({
  inputs = LONG,
  takeProfit = null,
  enteredQty = null,
  ...rest
}: {
  inputs?: SizingInputs;
  takeProfit?: number | null;
  enteredQty?: number | null;
  onPickTarget?: (p: string) => void;
  onUseShares?: (n: number) => void;
  formatSharesLabel?: (n: number) => string;
  disabled?: boolean;
} = {}) {
  const sizing = computeSizing(inputs);
  const score =
    sizing === null || takeProfit === null || inputs.entry === null
      ? null
      : scoreTakeProfit({
          side: inputs.side,
          entry: inputs.entry,
          takeProfit,
          riskPerShare: sizing.riskPerShare,
          shares: enteredQty ?? sizing.wholeShares,
        });

  return render(
    <PositionSizingPanel
      sizing={sizing}
      hint={sizingHint(inputs)}
      side={inputs.side}
      takeProfit={takeProfit}
      takeProfitScore={score}
      enteredQty={enteredQty}
      onPickTarget={rest.onPickTarget ?? (() => {})}
      {...rest}
    />
  );
}

/** The ladder chips, in the order they are rendered. */
const ladder = () =>
  screen
    .getAllByRole('button', { name: /^Set take profit to/ })
    .map((b) => b.getAttribute('aria-label'));

describe('when the inputs cannot be sized', () => {
  it('says why, rather than rendering an empty panel', () => {
    // The pairing the library's own tests pin: a null result always has a
    // reason, and this is the component that has to show it.
    renderPanel({ inputs: { ...LONG, stop: 105 } });

    expect(screen.getByText(/stop must sit below the entry/)).toBeInTheDocument();
    expect(screen.queryByText('1R / share')).not.toBeInTheDocument();
  });
});

describe('the R ladder', () => {
  it('offers 1R, 2R, 3R and 5R', () => {
    // 5R is the rung that was added when the ladder was widened. A trade that
    // only a runner reaches is a different decision from 3R, and dropping it
    // quietly frames three as the ceiling.
    renderPanel();

    expect(ladder()).toEqual([
      'Set take profit to 105.00 (1R)',
      'Set take profit to 110.00 (2R)',
      'Set take profit to 115.00 (3R)',
      'Set take profit to 125.00 (5R)',
    ]);
  });

  it('runs the ladder downwards on a short', () => {
    renderPanel({
      inputs: { ...LONG, side: 'SELL', entry: 100, stop: 105 },
    });

    expect(ladder()).toEqual([
      'Set take profit to 95.00 (1R)',
      'Set take profit to 90.00 (2R)',
      'Set take profit to 85.00 (3R)',
      'Set take profit to 75.00 (5R)',
    ]);
  });

  it('hands the host a price it can write straight into a number field', () => {
    // Not the raw float. The host holds prices as strings, and 0.5009999...
    // is what reaches the field if the component passes the number through.
    const onPickTarget = vi.fn();
    renderPanel({
      inputs: { ...LONG, entry: 0.5, stop: 0.499 },
      onPickTarget,
    });

    fireEvent.click(screen.getByRole('button', { name: /\(2R\)$/ }));

    expect(onPickTarget).toHaveBeenCalledWith('0.5020');
  });

  it('keeps a sub-dollar ladder legible instead of collapsing it', () => {
    // At two decimals every rung of this ladder reads "0.50" and the ladder
    // looks like it has no spacing at all.
    renderPanel({ inputs: { ...LONG, entry: 0.5, stop: 0.499 } });

    expect(ladder()).toEqual([
      'Set take profit to 0.5010 (1R)',
      'Set take profit to 0.5020 (2R)',
      'Set take profit to 0.5030 (3R)',
      'Set take profit to 0.5050 (5R)',
    ]);
  });

  it('marks the rung the host has actually chosen', () => {
    renderPanel({ takeProfit: 110 });

    expect(screen.getByRole('button', { name: /\(2R\)$/ })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
    expect(screen.getByRole('button', { name: /\(3R\)$/ })).toHaveAttribute(
      'aria-pressed',
      'false'
    );
  });

  it('shows target prices even when the account size is unknown', () => {
    // Where 2R sits does not depend on how much is being risked. Only the
    // money columns go blank.
    renderPanel({ inputs: { ...LONG, accountSize: null } });

    expect(ladder()).toHaveLength(4);
    expect(screen.getByText('$5.00')).toBeInTheDocument(); // 1R / share
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });
});

describe('the share count', () => {
  it('shows the whole shares alongside the fraction they were floored from', () => {
    // 100 / 15 = 6.67. Showing only "6" hides how much of the risk budget is
    // going unused; showing only "6.67" suggests you can buy it.
    renderPanel({ inputs: { ...LONG, stop: 85 } });

    expect(screen.getByText('6')).toBeInTheDocument();
    expect(screen.getByText('(6.67)')).toBeInTheDocument();
  });

  it('warns when the risk budget will not cover a single share', () => {
    renderPanel({ inputs: { ...LONG, entry: 500, stop: 350 } });

    expect(
      screen.getByText(/Risk budget is smaller than one share/)
    ).toBeInTheDocument();
  });

  it('warns when the position costs more than the account holds', () => {
    // A fraction over 1 is legal — the account has margin — but it should be
    // a decision rather than something noticed after the fill.
    renderPanel({ inputs: { ...LONG, stop: 99.5, riskPercent: 5 } });

    expect(screen.getByText(/needs margin/)).toBeInTheDocument();
  });

  it('offers the suggestion as a button only when a host can receive it', () => {
    const onUseShares = vi.fn();
    const { unmount } = renderPanel({ onUseShares });

    fireEvent.click(screen.getByRole('button', { name: 'Use 20 shares' }));
    expect(onUseShares).toHaveBeenCalledWith(20);

    unmount();
    renderPanel();
    expect(screen.queryByRole('button', { name: /^Use /})).not.toBeInTheDocument();
  });

  it('says "1 share", not "1 shares"', () => {
    // A $1,000 budget against $600 of risk per share floors to exactly one.
    renderPanel({
      inputs: { ...LONG, entry: 1000, stop: 400, riskPercent: 10 },
      onUseShares: vi.fn(),
    });

    expect(screen.getByRole('button', { name: 'Use 1 share' })).toBeInTheDocument();
  });
});

describe('the typed take profit', () => {
  it('scores a target that sits between two rungs', () => {
    // The question you actually arrive with: out at 107, what is that worth?
    renderPanel({ takeProfit: 107, enteredQty: 20 });

    expect(screen.getByText('Your take profit')).toBeInTheDocument();
    expect(screen.getByText(/1\.40R/)).toBeInTheDocument();
    expect(screen.getByText(/\$7\.00 per share on 20 shares/)).toBeInTheDocument();
  });

  it('names a backwards target instead of reporting a negative R', () => {
    // A long taking profit below where it bought is a typo, not a strategy,
    // and "-1.00R" reads like a deliberate choice.
    renderPanel({ takeProfit: 95 });

    expect(screen.getByText(/on the losing side of your entry/)).toBeInTheDocument();
    expect(screen.getByText(/for a long it has to sit above it/)).toBeInTheDocument();
    expect(screen.queryByText(/-1\.00R/)).not.toBeInTheDocument();
  });

  it('flips that message for a short', () => {
    renderPanel({
      inputs: { ...LONG, side: 'SELL', entry: 100, stop: 105 },
      takeProfit: 110,
    });

    expect(screen.getByText(/for a short it has to sit below it/)).toBeInTheDocument();
  });

  it('discloses when a profit is quoted on the suggested size, not a real one', () => {
    // Otherwise the figure reads as what the trade will pay, when it is what
    // the trade would pay at a quantity the user has not agreed to.
    renderPanel({ takeProfit: 107, enteredQty: null });

    expect(screen.getByText(/\(suggested size\)/)).toBeInTheDocument();
  });

  it('drops that note once a real quantity is entered', () => {
    renderPanel({ takeProfit: 107, enteredQty: 5 });

    expect(screen.queryByText(/\(suggested size\)/)).not.toBeInTheDocument();
    expect(screen.getByText(/on 5 shares/)).toBeInTheDocument();
  });
});

describe('while the host is saving', () => {
  it('disables the ladder and the suggestion, so nothing is picked mid-flight', () => {
    renderPanel({ disabled: true, onUseShares: vi.fn() });

    for (const chip of screen.getAllByRole('button', { name: /^Set take profit/ })) {
      expect(chip).toBeDisabled();
    }
    expect(screen.getByRole('button', { name: 'Use 20 shares' })).toBeDisabled();
  });
});

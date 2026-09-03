/**
 * ScaledEntryPlanner: several buys into one position, inside one risk budget.
 *
 * The arithmetic itself is tested exhaustively in positionSizing.test.ts, which
 * is held to literal 100% branch coverage. What is worth asserting HERE is the
 * wiring the arithmetic cannot reach: that the rungs the user types become the
 * tranches the library sees, that the numbers it returns are the numbers on
 * screen, and above all that `onApply` hands back the blend the plan will
 * actually be saved with -- formatted, because the field it lands in holds a
 * string and the column behind it holds four decimals.
 *
 * A pure controlled component: no queries, no context, no wrapper needed.
 */

import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ScaledEntryPlanner } from '@/components/ScaledEntryPlanner';
import type { EntryTranche } from '@/lib/positionSizing';

afterEach(() => cleanup());

/**
 * A long with a $100 budget: rungs at 100 and 95 against a stop at 90 risk $10
 * and $5 a share, so riskPerUnit is $7.50, the ladder buys 6 + 6, and the
 * blend lands at 97.50 risking $90 of the $100.
 */
const LADDER: EntryTranche[] = [
  { percent: 50, price: 100 },
  { percent: 50, price: 95 },
];

function mount(props: Partial<React.ComponentProps<typeof ScaledEntryPlanner>> = {}) {
  const onChange = props.onChange ?? vi.fn();
  const onApply = props.onApply ?? vi.fn();
  render(
    <ScaledEntryPlanner
      side="BUY"
      stop={90}
      accountSize={10_000}
      riskPercent={1}
      tranches={LADDER}
      onChange={onChange}
      onApply={onApply}
      {...props}
    />
  );
  return { onChange, onApply };
}

describe('with no rungs yet', () => {
  it('explains itself rather than showing an empty grid', () => {
    mount({ tranches: [] });

    expect(screen.getByText(/Buy part of the position at one price/)).toBeInTheDocument();
    expect(screen.queryByLabelText('Entry price, rung 1')).not.toBeInTheDocument();
  });

  it('appends a blank rung on Add', () => {
    const { onChange } = mount({ tranches: [] });

    fireEvent.click(screen.getByRole('button', { name: /Add a rung/ }));
    expect(onChange).toHaveBeenCalledWith([{ percent: 0, price: 0 }]);
  });
});

describe('a priced ladder', () => {
  it('shows each rung its own risk per share and share count', () => {
    mount();

    // The point of the whole component: 10 and 5, not one figure twice.
    expect(screen.getByText('$10.00')).toBeInTheDocument();
    expect(screen.getByText('$5.00')).toBeInTheDocument();
    // 6 shares a rung, risking $60 and $30.
    expect(screen.getAllByText('6 sh')).toHaveLength(2);
    // $60 twice, and necessarily so: rung 1's risk IS what fills if only
    // rung 1 fills, so the row and the partial-fill line below agree.
    expect(screen.getAllByText(/\$60\.00/)).toHaveLength(2);
    expect(screen.getByText(/\$30\.00/)).toBeInTheDocument();
  });

  it('reports the blend, and the budget it came in under', () => {
    mount();

    expect(screen.getByText('Blended entry')).toBeInTheDocument();
    expect(screen.getByText('97.50')).toBeInTheDocument();
    // Twice: once in the summary line, once on the button that applies it.
    expect(screen.getAllByText(/× 12 sh/)).toHaveLength(2);
    // $90 risked of a $100 budget -- the shortfall flooring leaves, stated
    // rather than left to be inferred from the share count.
    expect(screen.getByText(/\$90\.00 \(0\.90%\)/)).toBeInTheDocument();
    expect(screen.getByText(/of \$100\.00/)).toBeInTheDocument();
  });

  it('says what fills if only the first rung does', () => {
    mount();

    // A ladder is a conditional plan; the headline 0.90% assumes all of it.
    expect(screen.getByText(/If only rung 1 fills/)).toBeInTheDocument();
    expect(screen.getByText(/\(0\.60%\)/)).toBeInTheDocument();
  });

  it('hands back the blended entry and total shares together', () => {
    const { onApply } = mount();

    fireEvent.click(screen.getByRole('button', { name: /Use 97\.50 × 12 shares/ }));
    // Formatted, because it goes into a string field and a NUMERIC(10,4)
    // column -- not 97.5 as a raw float.
    expect(onApply).toHaveBeenCalledWith('97.50', 12);
  });

  it('edits a rung in place and drops one on request', () => {
    const { onChange } = mount();

    fireEvent.change(screen.getByLabelText('Entry price, rung 2'), {
      target: { value: '96' },
    });
    expect(onChange).toHaveBeenCalledWith([
      { percent: 50, price: 100 },
      { percent: 50, price: 96 },
    ]);

    fireEvent.click(screen.getByRole('button', { name: 'Remove rung 1' }));
    expect(onChange).toHaveBeenCalledWith([{ percent: 50, price: 95 }]);
  });
});

describe('ladders that cannot be sized', () => {
  it('needs a stop before it can price anything', () => {
    mount({ stop: null });

    expect(screen.getByText(/Enter a stop above/)).toBeInTheDocument();
    expect(screen.queryByText('Blended entry')).not.toBeInTheDocument();
  });

  it('refuses to size a rung on the losing side of the stop', () => {
    mount({
      tranches: [
        { percent: 50, price: 100 },
        { percent: 50, price: 85 },
      ],
    });

    expect(
      screen.getByText(/for a long every entry has to be above it/)
    ).toBeInTheDocument();
    // No share count offered, and nothing to apply -- a negative risk per
    // share would have bought MORE than the budget allows.
    expect(screen.queryByRole('button', { name: /^Use / })).not.toBeInTheDocument();
    expect(screen.queryByText(/× \d+ sh/)).not.toBeInTheDocument();
  });

  it('names the inverted stop for a short in the short direction', () => {
    mount({
      side: 'SELL',
      stop: 90,
      tranches: [{ percent: 100, price: 100 }],
    });

    expect(
      screen.getByText(/for a short every entry has to be below it/)
    ).toBeInTheDocument();
  });

  it('flags rungs adding up to more of the position than exists', () => {
    mount({
      tranches: [
        { percent: 70, price: 100 },
        { percent: 70, price: 95 },
      ],
    });

    expect(screen.getByText(/140% of a position you only buy 100% of/)).toBeInTheDocument();
  });

  it('notes when the rungs do not add up to the whole position', () => {
    mount({ tranches: [{ percent: 60, price: 100 }] });

    expect(
      screen.getByText(/add up to 60% — the ladder is sized as though that is the whole position/)
    ).toBeInTheDocument();
  });

  it('names a rung too small to buy a whole share', () => {
    mount({
      tranches: [
        { percent: 99, price: 100 },
        { percent: 1, price: 95 },
      ],
    });

    expect(screen.getByText(/buys no whole shares/)).toBeInTheDocument();
  });

  it('prices the rungs but offers no size without an account', () => {
    mount({ accountSize: null });

    // The blend is still worth knowing -- it is the percent-weighted intent.
    expect(screen.getByText('97.50')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Use / })).not.toBeInTheDocument();
    expect(screen.queryByText(/Risk, all rungs filled/)).not.toBeInTheDocument();
  });

  it('warns when the filled ladder would cost more than the account holds', () => {
    // A tight stop makes each share cheap in risk terms, so a 1% budget buys
    // a position worth many times the account.
    mount({ accountSize: 10_000, riskPercent: 1, stop: 99.9, tranches: [{ percent: 100, price: 100 }] });

    expect(screen.getByText(/needs margin/)).toBeInTheDocument();
  });

  it('disables every control while the host is saving', () => {
    mount({ disabled: true });

    expect(screen.getByLabelText('Entry price, rung 1')).toBeDisabled();
    expect(screen.getByRole('button', { name: /Add a rung/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Remove rung 1' })).toBeDisabled();
    expect(screen.getByRole('button', { name: /^Use / })).toBeDisabled();
  });
});

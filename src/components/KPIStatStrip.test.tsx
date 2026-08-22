/**
 * KPIStatStrip: scoped to the Avg R card only.
 *
 * The rest of this component (Net P&L, Win Rate, Total Trades, Profit
 * Factor, Avg ROI, Inbox Queue) has no test coverage of its own, and this
 * file does not attempt to add it -- that would be a much larger, separate
 * undertaking than the one new card this suite exists to verify. Avg R is
 * pure presentation (a `KPIStats` prop straight to JSX, no hooks, no query),
 * so it is cheap to test in isolation without mounting the dashboard page
 * that actually produces the prop.
 */

import React from 'react';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { KPIStatStrip } from '@/components/KPIStatStrip';
import type { KPIStats } from '@/types/api';

afterEach(cleanup);

function stats(overrides: Partial<KPIStats> = {}): KPIStats {
  return {
    netPnl: 0,
    grossPnl: 0,
    totalCommission: 0,
    ibCommission: 0,
    unverifiedLegs: 0,
    openRunPnl: 0,
    winRate: 0,
    totalTrades: 0,
    profitFactor: 0,
    avgRoi: 0,
    avgR: null,
    avgRSample: 0,
    pendingCount: 0,
    ...overrides,
  };
}

function avgRCard() {
  return within(screen.getByText('Avg R').closest('div.rounded-xl') as HTMLElement);
}

describe('KPIStatStrip > Avg R card', () => {
  it('renders a dash, in neutral color, when avgR is null', async () => {
    render(<KPIStatStrip stats={stats({ avgR: null, avgRSample: 0 })} />);
    const value = await avgRCard().findByText('—');
    expect(value).toHaveClass('text-white');
  });

  it('renders a signed positive value in the win color', async () => {
    render(<KPIStatStrip stats={stats({ avgR: 0.42, avgRSample: 5 })} />);
    const value = await avgRCard().findByText('+0.42R');
    expect(value).toHaveClass('text-win');
  });

  it('renders a signed negative value in the loss color', async () => {
    render(<KPIStatStrip stats={stats({ avgR: -0.42, avgRSample: 5 })} />);
    const value = await avgRCard().findByText('-0.42R');
    expect(value).toHaveClass('text-loss');
  });

  it('renders exactly zero (not null) with a + sign, in neutral color -- same >= 0 convention as RBadge', async () => {
    render(<KPIStatStrip stats={stats({ avgR: 0, avgRSample: 5 })} />);
    const value = await avgRCard().findByText('+0.00R');
    expect(value).toHaveClass('text-white');
  });

  it('pluralizes the sample-size hint at 0 or 2+, and keeps it singular at exactly 1', async () => {
    const { rerender } = render(<KPIStatStrip stats={stats({ avgRSample: 0 })} />);
    expect(await avgRCard().findByText('0 scored trades')).toBeInTheDocument();

    rerender(<KPIStatStrip stats={stats({ avgR: 1, avgRSample: 1 })} />);
    expect(await avgRCard().findByText('1 scored trade')).toBeInTheDocument();

    rerender(<KPIStatStrip stats={stats({ avgR: 1, avgRSample: 2 })} />);
    expect(await avgRCard().findByText('2 scored trades')).toBeInTheDocument();
  });
});

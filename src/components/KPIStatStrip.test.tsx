/**
 * KPIStatStrip: scoped to the two merged cards from the Phase 3 redesign
 * (Win Rate + Total Trades, and the Profit Factor / Avg ROI / Avg R "Trade
 * Quality" card) plus the pre-existing Avg R coverage that card absorbed.
 *
 * Net P&L has no test coverage of its own, and this file does not attempt
 * to add it -- that would be a much larger, separate undertaking than the
 * merge this suite exists to verify. Both merged cards are pure
 * presentation (a `KPIStats` prop straight to JSX, no hooks, no query), so
 * they are cheap to test in isolation without mounting the dashboard page
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
    ...overrides,
  };
}

function winRateCard() {
  return within(screen.getByText('Win Rate').closest('div.rounded-xl') as HTMLElement);
}

/** Scopes to the merged "Trade Quality" card -- Profit Factor, Avg ROI, and
 * Avg R all live inside it, so any of their own labels works as the anchor. */
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

describe('KPIStatStrip > Win Rate card (merged with Total Trades)', () => {
  it('renders the win rate headline and the trade count as its qualifier', async () => {
    render(<KPIStatStrip stats={stats({ winRate: 34.56, totalTrades: 136 })} />);
    const card = winRateCard();
    expect(await card.findByText('34.56%')).toBeInTheDocument();
    expect(await card.findByText('136 trades')).toBeInTheDocument();
  });

  it('keeps the trade count singular at exactly 1, same convention as the scored-trades hint', async () => {
    render(<KPIStatStrip stats={stats({ totalTrades: 1 })} />);
    expect(await winRateCard().findByText('1 trade')).toBeInTheDocument();
  });

  it("does not also render 'Sample Size' -- Total Trades' old standalone caption is gone, not duplicated", async () => {
    render(<KPIStatStrip stats={stats({ totalTrades: 5 })} />);
    expect(screen.queryByText('Sample Size')).not.toBeInTheDocument();
  });
});

describe('KPIStatStrip > Trade Quality card (merged Profit Factor / Avg ROI / Avg R)', () => {
  it('renders all three rows with their own values and qualifiers', async () => {
    render(
      <KPIStatStrip
        stats={stats({ profitFactor: 1.85, avgRoi: 3.2, avgR: 0.45, avgRSample: 28 })}
      />
    );
    const card = avgRCard();
    expect(await card.findByText('1.85')).toBeInTheDocument();
    expect(await card.findByText('Gross win $ / loss $')).toBeInTheDocument();
    expect(await card.findByText('+3.20%')).toBeInTheDocument();
    expect(await card.findByText('+0.45R')).toBeInTheDocument();
  });

  it('renders infinity, not a number, for a null profit factor', async () => {
    render(<KPIStatStrip stats={stats({ profitFactor: null })} />);
    expect(await avgRCard().findByText('∞')).toBeInTheDocument();
  });

  it('captions Avg ROI as "Capital-weighted", not the old, wrong "Per Execution" (avg_roi_pct sums whole positions, not fills)', async () => {
    render(<KPIStatStrip stats={stats({ avgRoi: 1 })} />);
    expect(await avgRCard().findByText('Capital-weighted')).toBeInTheDocument();
    expect(screen.queryByText('Per Execution')).not.toBeInTheDocument();
  });

  it('does not render Profit Factor, Avg ROI, or Avg R as their own separate cards anymore', () => {
    render(<KPIStatStrip stats={stats({})} />);
    expect(screen.queryByText('Profit Factor ($)')?.closest('div.rounded-xl')).toBe(
      screen.queryByText('Avg Trade ROI')?.closest('div.rounded-xl')
    );
    expect(screen.queryByText('Avg Trade ROI')?.closest('div.rounded-xl')).toBe(
      screen.queryByText('Avg R')?.closest('div.rounded-xl')
    );
  });
});

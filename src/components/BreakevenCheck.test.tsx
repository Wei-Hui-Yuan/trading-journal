/**
 * BreakevenCheck: the arithmetic floor for a target, against the rate the
 * trader actually achieves.
 *
 * The case that matters most here is the empty journal.
 * `/api/analytics/advanced` returns `win_rate_pct: 0.0` when nothing could be
 * scored -- unlike `avg_r`, which correctly returns null. Read on its own,
 * that field cannot tell "no history" apart from "lost every trade", and
 * rendering the zero would state the second while meaning the first. Several
 * tests below exist only to hold the `scored_trades` gate in place.
 */

import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  getAdvancedMetrics: vi.fn(),
}));

import { BreakevenCheck } from '@/components/BreakevenCheck';
import { getAdvancedMetrics } from '@/lib/api';
import type { AdvancedMetrics } from '@/types/api';

const mockMetrics = vi.mocked(getAdvancedMetrics);

/** Only the fields this component reads; the rest of the payload is inert. */
const metrics = (over: Partial<AdvancedMetrics> = {}): AdvancedMetrics =>
  ({
    scored_trades: 89,
    unscored_trades: 0,
    total_r: 0,
    avg_r: 1.6,
    win_rate_pct: 41,
    profit_factor_r: null,
    expectancy_r: null,
    avg_slippage: null,
    slippage_sample: 0,
    r_distribution: {},
    mistake_breakdown: [],
    discipline_breakdown: [],
    compliance_buckets: [],
    strategy_breakdown: [],
    ...over,
  }) as AdvancedMetrics;

beforeEach(() => {
  vi.clearAllMocks();
  mockMetrics.mockResolvedValue(metrics());
});

afterEach(cleanup);

function renderCheck(rMultiple: number | null) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <BreakevenCheck rMultiple={rMultiple} />
    </QueryClientProvider>
  );
}

describe('the breakeven figure', () => {
  it.each([
    [1, '50%'],
    [2, '33%'],
    [3, '25%'],
    [5, '17%'],
  ])('puts %sR at %s', async (r, expected) => {
    renderCheck(r);

    expect(await screen.findByText(expected)).toBeInTheDocument();
  });

  it('names the R it is talking about', async () => {
    renderCheck(2.4);

    expect(await screen.findByText(/of 2\.40R trades/)).toBeInTheDocument();
  });

  it('says the floor is before costs, because it is', async () => {
    // It assumes every loss is a full 1R and every win reaches the target
    // exactly, and counts no commission or slippage. The real bar is higher.
    renderCheck(2);

    expect(await screen.findByText(/Before costs/)).toBeInTheDocument();
  });

  it.each([
    ['no target', null],
    ['a backwards target', -1],
    ['a target at the entry', 0],
  ])('renders nothing for %s', (_label, r) => {
    // The panel already names a backwards target as the typo it is. Repeating
    // it here as a win-rate problem would misdescribe it.
    const { container } = renderCheck(r);

    expect(container).toBeEmptyDOMElement();
  });
});

describe('the comparison against real history', () => {
  it('reports the achieved rate and the population behind it', async () => {
    renderCheck(2);

    expect(
      await screen.findByText(/across 89 scored trades/)
    ).toBeInTheDocument();
    expect(screen.getByText('41%')).toBeInTheDocument();
  });

  it('says there is no history rather than reporting a 0% win rate', async () => {
    // THE case. The endpoint sends win_rate_pct: 0.0 for an empty journal,
    // which is indistinguishable from losing every trade unless
    // scored_trades is consulted.
    mockMetrics.mockResolvedValue(metrics({ scored_trades: 0, win_rate_pct: 0 }));
    renderCheck(2);

    expect(
      await screen.findByText(/No scored history yet to compare against/)
    ).toBeInTheDocument();
    expect(screen.queryByText('0%')).not.toBeInTheDocument();
  });

  it('still reports a genuine 0% when trades were actually scored', async () => {
    // The other half of the gate. Losing all eleven is a real result and
    // must not be hidden behind the same message as having no data.
    mockMetrics.mockResolvedValue(metrics({ scored_trades: 11, win_rate_pct: 0 }));
    renderCheck(2);

    expect(await screen.findByText('0%')).toBeInTheDocument();
    expect(
      screen.queryByText(/No scored history/)
    ).not.toBeInTheDocument();
  });

  it('calls out a thin sample', async () => {
    // 40% over five trades and over five hundred are different claims; the
    // screen should not render them identically.
    mockMetrics.mockResolvedValue(metrics({ scored_trades: 5, win_rate_pct: 40 }));
    renderCheck(2);

    expect(await screen.findByText(/a thin sample/)).toBeInTheDocument();
  });

  it('does not call a substantial sample thin', async () => {
    renderCheck(2);

    await screen.findByText(/across 89 scored trades/);
    expect(screen.queryByText(/thin sample/)).not.toBeInTheDocument();
  });

  it('says "1 scored trade", not "1 scored trades"', async () => {
    mockMetrics.mockResolvedValue(metrics({ scored_trades: 1, win_rate_pct: 100 }));
    renderCheck(2);

    expect(await screen.findByText(/across 1 scored trade —/)).toBeInTheDocument();
  });

  it('waits for the metrics rather than claiming no history while loading', async () => {
    // A pending query has scored_trades undefined. Rendering "no history" for
    // that moment would be a claim about the journal, not about the request.
    let resolve!: (m: AdvancedMetrics) => void;
    mockMetrics.mockReturnValue(
      new Promise<AdvancedMetrics>((r) => {
        resolve = r;
      })
    );
    renderCheck(2);

    // The floor is knowable immediately; the comparison is not.
    expect(await screen.findByText('33%')).toBeInTheDocument();
    // The assertion this test exists for. Saying "no scored history" here
    // would describe the pending request, not the journal.
    expect(screen.queryByText(/No scored history/)).not.toBeInTheDocument();

    resolve(metrics());
    expect(
      await screen.findByText(/across 89 scored trades/)
    ).toBeInTheDocument();
  });
});

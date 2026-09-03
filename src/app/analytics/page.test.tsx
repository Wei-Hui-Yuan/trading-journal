/**
 * AnalyticsPage: metrics KPIs, five breakdown cards, the pending review
 * queue, and the review drawer that empties it.
 *
 * None of the eight in-file pieces (`metric`, `KpiCard`, `RDistribution`,
 * `StrategyBreakdownChart`, `DisciplineBreakdown`, `ComplianceBuckets`,
 * `MistakeBreakdown`, `ReviewDrawer`) are exported, so every test mounts the
 * real default-exported `AnalyticsPage`.
 *
 * Only three `@/lib/api` functions are mocked -- `getAdvancedMetrics`,
 * `getPendingPositions`, `updatePositionReview` -- matching the fact that
 * `page.tsx` calls none of them directly; everything routes through
 * `useAdvancedMetrics` / `usePendingPositions` / `useReviewPosition`, which
 * run for REAL against a real `QueryClient`, exactly as in
 * `TradeInboxQueue.test.tsx`.
 *
 * `TimeframeToolbar` is mocked wholesale with a stand-in exposing its three
 * inbound props (`selection` is not needed by any assertion here and is
 * therefore not surfaced) plus a couple of buttons that call `onSelect`.
 * Its own internals -- `useTimeframes`, the add/edit modal, delete
 * confirmation, `getTimeframes`/`createTimeframe`/`updateTimeframe`/
 * `deleteTimeframe` -- belong to that component's own suite.
 *
 * No ResizeObserver polyfill and no recharts stub: `page.tsx` imports no
 * `recharts` at all, and both hand-rolled charts (`RDistribution`,
 * `StrategyBreakdownChart`) compute their `${n}%` widths from arithmetic on
 * the fetched numbers, never from a measured DOM box. Verified empirically
 * with a throwaway probe mounting the real page with synthetic
 * `strategy_breakdown` rows: every bar's `style.width` was a well-formed
 * non-zero `NN%`, `section.querySelector('svg')` was `null` in both cards,
 * and both ran clean under jsdom with no "not implemented" warnings. The
 * probe was deleted once that was confirmed -- this file needs no
 * environment change beyond what `vitest.config.mts` already sets up.
 */

import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  getAdvancedMetrics: vi.fn(),
  getPendingPositions: vi.fn(),
  updatePositionReview: vi.fn(),
}));

// Stand-in for TimeframeToolbar. Exposes exactly the three props AnalyticsPage
// feeds it (`window`, `isFetching`, `onSelect`) as visible, queryable surface,
// per the brief's instruction to keep those inspectable from this suite.
vi.mock('@/components/TimeframeToolbar', () => {
  const DEFAULT_SELECTION = { kind: 'preset', preset: '1Y' };
  return {
    DEFAULT_SELECTION,
    TimeframeToolbar: (props: {
      selection: unknown;
      onSelect: (s: unknown) => void;
      window?: { start_date: string | null; end_date: string | null; closed_trades_in_window: number; closed_trades_total: number };
      isFetching?: boolean;
    }) => (
      <div data-testid="toolbar-stand-in">
        <span data-testid="toolbar-fetching">{String(!!props.isFetching)}</span>
        <span data-testid="toolbar-window">
          {props.window
            ? `${props.window.start_date ?? 'null'}..${props.window.end_date ?? 'null'}:${props.window.closed_trades_in_window}/${props.window.closed_trades_total}`
            : 'no-window'}
        </span>
        <button
          type="button"
          onClick={() => props.onSelect({ kind: 'preset', preset: 'YTD' })}
        >
          Select YTD
        </button>
        <button
          type="button"
          onClick={() =>
            props.onSelect({
              kind: 'custom',
              id: 'custom-1',
              start_date: '2026-01-01',
              end_date: '2026-06-30',
            })
          }
        >
          Select Custom
        </button>
      </div>
    ),
  };
});

import * as api from '@/lib/api';
import AnalyticsPage from '@/app/analytics/page';
import type {
  AdvancedMetrics,
  ComplianceBucket,
  DisciplineBreakdown as DisciplineBreakdownRow,
  MistakeBreakdown as MistakeBreakdownRow,
  Position,
  StrategyBreakdown as StrategyBreakdownRow,
  ToolbarWindow,
} from '@/types/api';

const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function toolbarWindow(overrides: Partial<ToolbarWindow> = {}): ToolbarWindow {
  return {
    start_date: '2026-01-01',
    end_date: '2026-06-30',
    closed_trades_in_window: 10,
    closed_trades_total: 12,
    ...overrides,
  };
}

/** Every field defaulted to an empty/neutral shape; override per test. */
function advancedMetrics(overrides: Partial<AdvancedMetrics> = {}): AdvancedMetrics {
  return {
    scored_trades: 0,
    unscored_trades: 0,
    total_r: 0,
    avg_r: null,
    win_rate_pct: 0,
    profit_factor_r: null,
    expectancy_r: null,
    avg_slippage: null,
    slippage_sample: 0,
    avg_journal_lag_hours: null,
    journal_lag_sample: 0,
    r_distribution: {},
    mistake_breakdown: [],
    discipline_breakdown: [],
    compliance_buckets: [],
    strategy_breakdown: [],
    window: toolbarWindow(),
    ...overrides,
  };
}

function strategyRow(overrides: Partial<StrategyBreakdownRow> = {}): StrategyBreakdownRow {
  return {
    strategy: 'Breakout',
    trade_count: 10,
    scored: 8,
    unscored: 2,
    total_r: 4,
    avg_r: 0.5,
    win_rate_pct: 60,
    best_r: 2,
    worst_r: -1,
    net_pnl: 400,
    first_traded: '2026-01-01T18:00:00Z',
    // Mid-day UTC, not a bare date: a bare '2026-02-01' parses as UTC
    // midnight, which the component's America/New_York formatter shifts
    // back to Jan 31 -- a real gotcha for whoever next relies on this
    // default, now that last_traded is actually rendered.
    last_traded: '2026-02-01T18:00:00Z',
    ...overrides,
  };
}

function disciplineRow(overrides: Partial<DisciplineBreakdownRow> = {}): DisciplineBreakdownRow {
  return {
    discipline: 'Waited for confirmation',
    followed: { trade_count: 8, win_rate_pct: 70, avg_r: 1.1, r_sample: 8 },
    not_followed: { trade_count: 2, win_rate_pct: 20, avg_r: -0.5, r_sample: 2 },
    edge_win_rate_pct: 50,
    edge_r: 1.6,
    sample: 10,
    ...overrides,
  };
}

function complianceRow(overrides: Partial<ComplianceBucket> = {}): ComplianceBucket {
  return {
    compliance: '100%',
    trade_count: 5,
    win_rate_pct: 68,
    avg_r: 1.2,
    r_sample: 5,
    ...overrides,
  };
}

function mistakeRow(overrides: Partial<MistakeBreakdownRow> = {}): MistakeBreakdownRow {
  return {
    mistake: 'FOMO',
    trade_count: 3,
    scored: 3,
    unscored: 0,
    total_r: -2.4,
    avg_r: -0.8,
    win_rate_pct: 10,
    ...overrides,
  };
}

// Mirrors the non-exported MISTAKE_TAGS constant at page.tsx:33-42 (there's
// nothing to import -- see the file header: none of the eight in-file
// pieces are exported). Kept in sync by hand; if this list and the
// component's list drift, the Finding #10 test below would start failing
// the "every button aria-pressed=false" assertion against a real tag name.
const MISTAKE_TAGS_FOR_TEST = [
  'FOMO',
  'Chased',
  'Early Liquidation',
  'Moved Stop',
  'Oversized',
  'No Plan',
  'Revenge Trade',
  'Hesitated',
];

function position(overrides: Partial<Position> = {}): Position {
  return {
    id: 'pos-1',
    symbol: 'AAPL',
    style: 'SWING',
    quantity: 100,
    entry_price: 150,
    exit_price: 155,
    entry_time: '2026-01-05T14:30:00Z',
    exit_time: '2026-01-06T15:00:00Z',
    realized_pnl: 500,
    gross_pnl: 510,
    commission: 10,
    strategy_id: null,
    review_status: 'pending',
    tag_hard_sl: null,
    tag_retest: null,
    tag_plan_compliant: null,
    trade_grade: null,
    notes: null,
    mistakes: [],
    review_went_well: null,
    review_went_wrong: null,
    review_lessons: null,
    disciplines: [],
    created_at: null,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

let client: QueryClient;

function wrapper({ children }: { children: React.ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

/** Mounts AnalyticsPage and waits past both queries' initial pending state. */
async function mountPage() {
  const utils = render(<AnalyticsPage />, { wrapper });
  await waitFor(() => {
    expect(screen.queryByText('Loading metrics…')).not.toBeInTheDocument();
    expect(screen.queryByText('Loading queue…')).not.toBeInTheDocument();
  });
  return utils;
}

/** The KPI card's value node, scoped by its label -- avoids collisions with
 * identical-looking numbers rendered elsewhere on the page (guardrail #6). */
function kpiValue(label: string): HTMLElement {
  const labelNode = screen.getByText(label);
  const card = labelNode.closest('div');
  if (!card) throw new Error(`No KPI card found for label "${label}".`);
  const value = card.querySelector('.font-mono.font-bold');
  if (!value) throw new Error(`No value node found in KPI card "${label}".`);
  return value as HTMLElement;
}

function sectionHeading(text: string): HTMLElement {
  const heading = screen.getByText(text);
  const card = heading.closest('div.rounded-xl');
  if (!card) throw new Error(`No card container found for heading "${text}".`);
  return card as HTMLElement;
}

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics());
  mocked.getPendingPositions.mockResolvedValue([]);
  mocked.updatePositionReview.mockImplementation(
    (id: string, payload: Record<string, unknown>) =>
      Promise.resolve(position({ id, ...payload }))
  );
});

afterEach(() => {
  cleanup();
  client.clear();
});

// ===========================================================================
// metrics query states
// ===========================================================================

describe('metrics query states', () => {
  it('shows a loading spinner and no KPI/chart cards while metrics are pending', async () => {
    mocked.getAdvancedMetrics.mockReturnValue(new Promise(() => {}));
    mocked.getPendingPositions.mockResolvedValue([]);
    render(<AnalyticsPage />, { wrapper });

    expect(screen.getByText('Loading metrics…')).toBeInTheDocument();
    expect(screen.queryByText('Total R')).not.toBeInTheDocument();
    expect(screen.queryByText('R-Distribution')).not.toBeInTheDocument();
  });

  it('shows the Error instance message when the metrics fetch rejects with an Error', async () => {
    mocked.getAdvancedMetrics.mockRejectedValue(new Error('metrics offline'));
    render(<AnalyticsPage />, { wrapper });

    expect(await screen.findByText('metrics offline')).toBeInTheDocument();
  });

  it('falls back to a generic message when the metrics fetch rejects with a non-Error', async () => {
    mocked.getAdvancedMetrics.mockImplementation(() => Promise.reject('boom'));
    render(<AnalyticsPage />, { wrapper });

    expect(await screen.findByText('Failed to load metrics.')).toBeInTheDocument();
  });

  it('renders the KPI strip and all five breakdown cards once metrics load', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics());
    await mountPage();

    expect(screen.getByText('Total R')).toBeInTheDocument();
    expect(screen.getByText('Expectancy')).toBeInTheDocument();
    expect(screen.getByText('Profit Factor (R)')).toBeInTheDocument();
    expect(screen.getByText('Win Rate')).toBeInTheDocument();
    expect(screen.getByText('Avg Slippage')).toBeInTheDocument();
    expect(screen.getByText('R-Distribution')).toBeInTheDocument();
    expect(screen.getByText('Performance by Mistake')).toBeInTheDocument();
    expect(screen.getByText('Which Strategies Are Working')).toBeInTheDocument();
    expect(screen.getByText('Does Following Your Rules Pay?')).toBeInTheDocument();
    expect(screen.getByText('Does Overall Compliance Pay?')).toBeInTheDocument();
  });
});

// ===========================================================================
// KpiCard / metric() formatting
// ===========================================================================

describe('KpiCard / metric() formatting', () => {
  it('Total R formats a real number to 2 digits with an R suffix', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ total_r: 5.5, scored_trades: 4 }));
    await mountPage();
    expect(kpiValue('Total R')).toHaveTextContent('5.50R');
  });

  it('Expectancy renders a dash for null', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ expectancy_r: null }));
    await mountPage();
    expect(kpiValue('Expectancy')).toHaveTextContent('—');
  });

  it('Win Rate formats a percentage to 2 digits with a % suffix', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ win_rate_pct: 62.5 }));
    await mountPage();
    expect(kpiValue('Win Rate')).toHaveTextContent('62.50%');
  });

  it('Profit Factor (R) formats a real number to 2 digits with no suffix', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ profit_factor_r: 2.4 }));
    await mountPage();
    expect(kpiValue('Profit Factor (R)')).toHaveTextContent('2.40');
  });

  // Finding #5 (page.tsx ~736-737): profit_factor_r === null deliberately
  // bypasses metric()'s own null convention and renders '∞', not '—' --
  // null means "no losing trades", and 0.00 or a dash would misstate that.
  // Pinned separately so a later "normalize this to use metric() everywhere"
  // cleanup breaks a test rather than silently changing the meaning.
  it('Profit Factor (R) renders the literal infinity glyph for null, not a dash (Finding #5)', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ profit_factor_r: null }));
    await mountPage();
    expect(kpiValue('Profit Factor (R)')).toHaveTextContent('∞');
    expect(kpiValue('Profit Factor (R)')).not.toHaveTextContent('—');
  });

  it('Avg Slippage renders a dash for null', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ avg_slippage: null }));
    await mountPage();
    expect(kpiValue('Avg Slippage')).toHaveTextContent('—');
  });

  it('Avg Slippage formats a real number to 4 digits with no suffix', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ avg_slippage: 0.0123, slippage_sample: 7 }));
    await mountPage();
    expect(kpiValue('Avg Slippage')).toHaveTextContent('0.0123');
  });

  it('Journal Lag renders a dash and says nothing is journaled yet, before anything is', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({ avg_journal_lag_hours: null, journal_lag_sample: 0 })
    );
    await mountPage();
    expect(kpiValue('Journal Lag')).toHaveTextContent('—');
    expect(screen.getByText('Nothing journaled yet')).toBeInTheDocument();
  });

  it('Journal Lag formats hours through the shared duration formatter, with its sample size', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({ avg_journal_lag_hours: 6.25, journal_lag_sample: 3 })
    );
    await mountPage();
    expect(kpiValue('Journal Lag')).toHaveTextContent('6.3h');
    expect(screen.getByText(/3 journaled · close to write-up/)).toBeInTheDocument();
  });

  it('Total R gets the win tone class when non-negative', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ total_r: 1 }));
    await mountPage();
    expect(kpiValue('Total R')).toHaveClass('text-win');
  });

  it('Total R gets the loss tone class when negative', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ total_r: -1 }));
    await mountPage();
    expect(kpiValue('Total R')).toHaveClass('text-loss');
  });

  it('Profit Factor (R) gets the neutral (default) tone class -- it never passes a tone prop', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ profit_factor_r: 1.5 }));
    await mountPage();
    expect(kpiValue('Profit Factor (R)')).toHaveClass('text-white');
  });

  it('renders the hint paragraph when hint is truthy', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ scored_trades: 3 }));
    await mountPage();
    expect(screen.getByText('3 scored trades')).toBeInTheDocument();
  });

  it('singularizes the scored-trades hint for exactly one', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ scored_trades: 1 }));
    await mountPage();
    expect(screen.getByText('1 scored trade')).toBeInTheDocument();
  });

  it('omits the hint paragraph entirely when hint is falsy (Avg Slippage title has no hint text when defaulted)', async () => {
    // Every KpiCard on this page is always given a hint string, so this pins
    // the *conditional rendering mechanism* itself: Win Rate's hint is a
    // constant, always-truthy string, so its hint paragraph is always
    // present -- there is no reachable falsy-hint KpiCard call site on this
    // page. Documented here rather than skipped, since `hint?:` in the type
    // signature still makes the branch exist even if this file never drives
    // it false.
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics());
    await mountPage();
    expect(screen.getByText('Of scored trades')).toBeInTheDocument();
  });
});

// ===========================================================================
// RDistribution
// ===========================================================================

describe('RDistribution', () => {
  it('sizes each bar width as (count / max) * 100, with max floored at 1', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({ r_distribution: { '<-2R': 2, '0R to 1R': 8, '1R to 2R': 4 } })
    );
    await mountPage();
    const card = sectionHeading('R-Distribution');

    const bars = within(card).getAllByText(/^\d+$/).map((countSpan) => {
      const row = countSpan.closest('div.flex');
      return row?.querySelector('.h-full') as HTMLElement;
    });
    // max = 8: widths are 2/8, 8/8, 4/8 == 25%, 100%, 50%
    expect(bars[0].style.width).toBe('25%');
    expect(bars[1].style.width).toBe('100%');
    expect(bars[2].style.width).toBe('50%');
  });

  it('floors max at 1 so a single non-zero bucket still renders a full-width bar', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({ r_distribution: { '0R to 1R': 1 } })
    );
    await mountPage();
    const card = sectionHeading('R-Distribution');
    const bar = card.querySelector('.h-full') as HTMLElement;
    expect(bar.style.width).toBe('100%');
  });

  it.each([
    ['<-2R', true],
    ['-1R to 0R', true],
    ['0R to 1R', false],
    ['1R to 2R', false],
  ])('bucket %s -> isLoss %s (startsWith "<" or "-")', async (bucket, isLoss) => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({ r_distribution: { [bucket]: 5 } })
    );
    await mountPage();
    const card = sectionHeading('R-Distribution');
    const bar = card.querySelector('.h-full') as HTMLElement;
    expect(bar.className).toContain(isLoss ? 'bg-loss/40' : 'bg-win/40');
  });

  it('renders a singular footnote for exactly one unscored trade', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ unscored_trades: 1 }));
    await mountPage();
    expect(
      screen.getByText(/1 trade not scored [\s\S]*still open, or/)
    ).toBeInTheDocument();
  });

  it('renders a plural footnote for more than one unscored trade', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ unscored_trades: 3 }));
    await mountPage();
    expect(
      screen.getByText(/3 trades not scored [\s\S]*still open, or/)
    ).toBeInTheDocument();
  });

  it('omits the footnote entirely when unscored_trades is 0', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ unscored_trades: 0 }));
    await mountPage();
    expect(screen.queryByText(/not scored/)).not.toBeInTheDocument();
  });

  // Finding #1 (page.tsx 74-112): every sibling breakdown card has an
  // explicit "no data yet" string for its empty case; RDistribution does
  // not -- an empty `r_distribution: {}` renders the header and a blank
  // body with no message at all. Pinned here as current behaviour, not
  // fixed, and flagged in the write-up as an inconsistency worth the file
  // owner's attention.
  it('renders a blank body with no "no data" message for an empty r_distribution (Finding #1)', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ r_distribution: {} }));
    await mountPage();
    const card = sectionHeading('R-Distribution');
    expect(within(card).queryByText(/no data/i)).not.toBeInTheDocument();
    expect(card.querySelector('.h-full')).not.toBeInTheDocument();
  });
});

// ===========================================================================
// StrategyBreakdownChart
// ===========================================================================

describe('StrategyBreakdownChart', () => {
  it('renders the empty-state message when strategy_breakdown is empty', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ strategy_breakdown: [] }));
    await mountPage();
    expect(screen.getByText(/No scored trades yet/)).toBeInTheDocument();
  });

  it('computes a symmetric axis as ceil(max(|total_r|, 1)) and renders all five ticks', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        strategy_breakdown: [
          strategyRow({ strategy: 'A', total_r: 2.4 }),
          strategyRow({ strategy: 'B', total_r: -1 }),
        ],
      })
    );
    await mountPage();
    // axis = ceil(2.4) = 3 -> ticks: -3R, -1.5R, 0R, +1.5R, +3R
    expect(screen.getByText('-3R')).toBeInTheDocument();
    expect(screen.getByText('-1.5R')).toBeInTheDocument();
    expect(screen.getByText('0R')).toBeInTheDocument();
    expect(screen.getByText('+1.5R')).toBeInTheDocument();
    expect(screen.getByText('+3R')).toBeInTheDocument();
  });

  it('positions a positive total_r bar on the left half (left: 50%)', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({ strategy_breakdown: [strategyRow({ strategy: 'A', total_r: 3 })] })
    );
    await mountPage();
    const card = sectionHeading('Which Strategies Are Working');
    const bar = card.querySelector('.absolute.inset-y-1') as HTMLElement;
    expect(bar.style.left).toBe('50%');
    expect(bar.style.right).toBe('');
    expect(bar.style.width).toBe('50%'); // axis === span === 3, half(3) = 50
  });

  it('positions a negative total_r bar on the right half (right: 50%)', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({ strategy_breakdown: [strategyRow({ strategy: 'A', total_r: -3 })] })
    );
    await mountPage();
    const card = sectionHeading('Which Strategies Are Working');
    const bar = card.querySelector('.absolute.inset-y-1') as HTMLElement;
    expect(bar.style.right).toBe('50%');
    expect(bar.style.left).toBe('');
  });

  // Finding #4 (page.tsx 167): isUnassigned is a hardcoded string match on
  // 'Unassigned', not a shared constant with whatever names the server.
  it('renders "Unassigned" as a plain span, not a Link (Finding #4 -- hardcoded string match)', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({ strategy_breakdown: [strategyRow({ strategy: 'Unassigned', trade_count: 5 })] })
    );
    await mountPage();
    const label = screen.getByText('Unassigned (5)');
    expect(label.tagName).toBe('SPAN');
    expect(label.closest('a')).toBeNull();
  });

  it('renders any other strategy name as a Link to /strategies', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({ strategy_breakdown: [strategyRow({ strategy: 'Breakout', trade_count: 5 })] })
    );
    await mountPage();
    const label = screen.getByText('Breakout (5)');
    expect(label.closest('a')).toHaveAttribute('href', '/strategies');
  });

  // Finding #9 (page.tsx 179-186, Review C / business-logic review): every
  // row's title attribute is worded as if the link opens THAT strategy
  // specifically ("Open "X" in the strategy playbook"), but href is the
  // identical static "/strategies" string for every row -- no id, query
  // param, or anchor -- and /strategies (src/app/strategies/page.tsx, the
  // only file under that route) is not a dynamic route and never reads a
  // strategy id from the URL. Two rows with different tooltip text navigate
  // to the exact same generic list page, which has no way to know which
  // strategy the click was about. Pinned here, not fixed, since fixing it
  // means either changing the tooltip wording or adding real deep-linking
  // support to /strategies -- a product decision outside this suite's scope.
  it('two different strategy rows promise two different destinations in their title, but share the identical static href (Finding #9 -- tooltip/destination mismatch)', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        strategy_breakdown: [
          strategyRow({ strategy: 'Momentum Breakout', trade_count: 3 }),
          strategyRow({ strategy: 'Reversal Fade', trade_count: 2 }),
        ],
      })
    );
    await mountPage();
    const linkA = screen.getByText('Momentum Breakout (3)').closest('a');
    const linkB = screen.getByText('Reversal Fade (2)').closest('a');
    expect(linkA).toHaveAttribute('title', 'Open "Momentum Breakout" in the strategy playbook');
    expect(linkB).toHaveAttribute('title', 'Open "Reversal Fade" in the strategy playbook');
    // Different promised destinations, but identical actual destination.
    expect(linkA).toHaveAttribute('href', '/strategies');
    expect(linkB).toHaveAttribute('href', '/strategies');
  });

  it.each<[Partial<StrategyBreakdownRow>, string]>([
    [
      { avg_r: null, win_rate_pct: null },
      'Solo: +1.00R over 4 scored trades',
    ],
    [
      { avg_r: 1.2, win_rate_pct: null },
      'Solo: +1.00R over 4 scored trades · avg 1.20R',
    ],
    [
      { avg_r: null, win_rate_pct: 50 },
      'Solo: +1.00R over 4 scored trades · win 50%',
    ],
    [
      { avg_r: 1.2, win_rate_pct: 50 },
      'Solo: +1.00R over 4 scored trades · avg 1.20R · win 50%',
    ],
  ])('builds the tooltip title from the conditional chain: %j -> %s', async (overrides, expected) => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        strategy_breakdown: [
          strategyRow({ strategy: 'Solo', total_r: 1, scored: 4, unscored: 0, ...overrides }),
        ],
      })
    );
    await mountPage();
    const card = sectionHeading('Which Strategies Are Working');
    const bar = card.querySelector('.absolute.inset-y-1') as HTMLElement;
    expect(bar.title).toBe(expected);
  });

  it('appends the unscored-count clause to the tooltip when a row has unscored trades', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        strategy_breakdown: [
          strategyRow({
            strategy: 'Solo',
            total_r: 1,
            scored: 4,
            unscored: 2,
            avg_r: null,
            win_rate_pct: null,
          }),
        ],
      })
    );
    await mountPage();
    const card = sectionHeading('Which Strategies Are Working');
    const bar = card.querySelector('.absolute.inset-y-1') as HTMLElement;
    expect(bar.title).toBe('Solo: +1.00R over 4 scored trades (2 unscored)');
  });

  it('renders the coverage footnote when some row has unscored > 0', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        strategy_breakdown: [
          strategyRow({ strategy: 'A', unscored: 3 }),
          strategyRow({ strategy: 'B', unscored: 0 }),
        ],
      })
    );
    await mountPage();
    expect(screen.getByText(/3 trades could not/)).toBeInTheDocument();
  });

  it('omits the coverage footnote when every row has unscored === 0', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        strategy_breakdown: [
          strategyRow({ strategy: 'A', unscored: 0 }),
          strategyRow({ strategy: 'B', unscored: 0 }),
        ],
      })
    );
    await mountPage();
    expect(screen.queryByText(/could not/)).not.toBeInTheDocument();
  });

  it('renders best/worst R and the last-traded date, formatted in market time, under the bar', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        strategy_breakdown: [
          strategyRow({
            strategy: 'Solo',
            best_r: 2.5,
            worst_r: -1.25,
            last_traded: '2026-03-10T18:00:00Z',
          }),
        ],
      })
    );
    await mountPage();
    expect(
      screen.getByText('best +2.50R · worst -1.25R · last traded Mar 10, 2026')
    ).toBeInTheDocument();
  });

  it('omits the best/worst clause but keeps last traded when best_r/worst_r are null', async () => {
    // Null together, exactly when the strategy has zero scored trades --
    // there is nothing to range over, but entry_time (and so last_traded)
    // does not require a stop, so the two are independent.
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        strategy_breakdown: [
          strategyRow({
            strategy: 'Solo',
            best_r: null,
            worst_r: null,
            last_traded: '2026-03-10T18:00:00Z',
          }),
        ],
      })
    );
    await mountPage();
    expect(screen.getByText('last traded Mar 10, 2026')).toBeInTheDocument();
    expect(screen.queryByText(/best/)).not.toBeInTheDocument();
  });

  it('renders no detail line at all when best_r, worst_r, and last_traded are all null', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        strategy_breakdown: [
          strategyRow({ strategy: 'Solo', best_r: null, worst_r: null, last_traded: null }),
        ],
      })
    );
    await mountPage();
    const card = sectionHeading('Which Strategies Are Working');
    // Checked structurally, not just by absent text: `detail.join(' · ')`
    // on an empty array is '', so a guard that rendered the wrapper
    // unconditionally would ALSO show no "best"/"last traded" text --
    // querying for the detail line's own uniquely-classed element is what
    // actually tells the two apart.
    expect(card.querySelector('[class*="mt-0.5"]')).toBeNull();
  });

  it('signs a positive worst_r with a leading + like best_r, when a strategy has never lost', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        strategy_breakdown: [
          strategyRow({ strategy: 'Solo', best_r: 3, worst_r: 0.2, last_traded: null }),
        ],
      })
    );
    await mountPage();
    expect(screen.getByText('best +3.00R · worst +0.20R')).toBeInTheDocument();
  });

  it('signs a negative best_r with no leading +, when even the best trade of a strategy lost', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        strategy_breakdown: [
          strategyRow({ strategy: 'Solo', best_r: -0.3, worst_r: -2, last_traded: null }),
        ],
      })
    );
    await mountPage();
    expect(screen.getByText('best -0.30R · worst -2.00R')).toBeInTheDocument();
  });
});

// ===========================================================================
// DisciplineBreakdown
// ===========================================================================

describe('DisciplineBreakdown', () => {
  it('renders the empty-state message when discipline_breakdown is empty', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ discipline_breakdown: [] }));
    await mountPage();
    expect(screen.getByText(/No discipline answers yet/)).toBeInTheDocument();
  });

  it.each<[number | null, string, string]>([
    [null, '—', 'No comparison available — every reviewed trade fell on one side of this rule'],
    [5, '+5 pts', 'Percentage points of win rate gained by following this rule'],
    [-5, '-5 pts', 'Percentage points of win rate gained by following this rule'],
  ])('edge_win_rate_pct %s renders "%s" with the matching title', async (edge, expectedText, expectedTitle) => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({ discipline_breakdown: [disciplineRow({ edge_win_rate_pct: edge })] })
    );
    await mountPage();
    const card = sectionHeading('Does Following Your Rules Pay?');
    const cell = within(card).getByText(expectedText);
    expect(cell).toHaveAttribute('title', expectedTitle);
  });

  it('gives a positive edge the win color and a negative edge the loss color', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        discipline_breakdown: [
          disciplineRow({ discipline: 'Rule A', edge_win_rate_pct: 5 }),
          disciplineRow({ discipline: 'Rule B', edge_win_rate_pct: -5 }),
        ],
      })
    );
    await mountPage();
    const card = sectionHeading('Does Following Your Rules Pay?');
    expect(within(card).getByText('+5 pts')).toHaveClass('text-win');
    expect(within(card).getByText('-5 pts')).toHaveClass('text-loss');
  });

  it("renders '—' for a null win_rate_pct in the followed/not_followed sub-columns", async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        discipline_breakdown: [
          disciplineRow({
            followed: { trade_count: 3, win_rate_pct: null, avg_r: null, r_sample: 0 },
            not_followed: { trade_count: 1, win_rate_pct: 40, avg_r: null, r_sample: 0 },
          }),
        ],
      })
    );
    await mountPage();
    const card = sectionHeading('Does Following Your Rules Pay?');
    // Exactly three dashes are producible from this fixture: the followed
    // side's null win_rate_pct, plus its avg R sub-line and the
    // not_followed side's avg R sub-line -- both null via r_sample: 0.
    // not_followed.win_rate_pct is 40 (not null), and edge_win_rate_pct/
    // edge_r are left at the row defaults of 50 and 1.6 (neither null), so a
    // pinned count of 3 -- not just "at least one" -- actually verifies the
    // null substitution landed on exactly these cells (Review B / coverage
    // review: >=1 would silently pass even if a regression also blanked an
    // unrelated cell).
    const dashes = within(card).getAllByText('—');
    expect(dashes.length).toBe(3);
    expect(within(card).getByText('40%')).toBeInTheDocument();
  });

  it.each<[number | null, string, string]>([
    [null, '—', 'No comparison available — every reviewed trade fell on one side of this rule'],
    [0.8, '+0.80R', 'R gained per trade by following this rule, against not following it'],
    [-0.8, '-0.80R', 'R gained per trade by following this rule, against not following it'],
  ])('edge_r %s renders "%s" with the matching title', async (edge, expectedText, expectedTitle) => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({ discipline_breakdown: [disciplineRow({ edge_r: edge })] })
    );
    await mountPage();
    const card = sectionHeading('Does Following Your Rules Pay?');
    const cell = within(card).getByText(expectedText);
    expect(cell).toHaveAttribute('title', expectedTitle);
  });

  it('gives a positive edge_r the win color and a negative one the loss color, independent of edge_win_rate_pct', async () => {
    // The two edges are independent figures -- a rule can win more often and
    // still cost more per trade, or the reverse -- so this pins the R edge's
    // own color logic rather than assuming it inherits the pts edge's.
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        discipline_breakdown: [
          disciplineRow({ discipline: 'Rule A', edge_win_rate_pct: -5, edge_r: 0.9 }),
          disciplineRow({ discipline: 'Rule B', edge_win_rate_pct: 5, edge_r: -0.9 }),
        ],
      })
    );
    await mountPage();
    const card = sectionHeading('Does Following Your Rules Pay?');
    expect(within(card).getByText('+0.90R')).toHaveClass('text-win');
    expect(within(card).getByText('-0.90R')).toHaveClass('text-loss');
  });

  it("renders each side's avg R sub-line under its win rate", async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        discipline_breakdown: [
          disciplineRow({
            followed: { trade_count: 8, win_rate_pct: 70, avg_r: 1.1, r_sample: 8 },
            not_followed: { trade_count: 2, win_rate_pct: 20, avg_r: -0.5, r_sample: 2 },
          }),
        ],
      })
    );
    await mountPage();
    const card = sectionHeading('Does Following Your Rules Pay?');
    // Sample size appended -- r_sample can be smaller than trade_count, and
    // the figure exists precisely so a reader can't mistake a tiny sample
    // for a confident average (see the ComplianceBuckets equivalent below).
    expect(within(card).getByText('+1.10R (n=8)')).toBeInTheDocument();
    expect(within(card).getByText('-0.50R (n=2)')).toBeInTheDocument();
  });

  it("falls back to '—' for a side's avg R when r_sample is 0, even if avg_r is somehow non-null", async () => {
    // Defends the SAME pairing ComplianceBuckets' own r_sample check
    // defends: trusting avg_r alone would render a number with nothing
    // behind it if the two fields ever disagreed.
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        discipline_breakdown: [
          disciplineRow({
            followed: { trade_count: 3, win_rate_pct: 70, avg_r: 1.1, r_sample: 0 },
          }),
        ],
      })
    );
    await mountPage();
    const card = sectionHeading('Does Following Your Rules Pay?');
    expect(within(card).queryByText('+1.10R')).not.toBeInTheDocument();
    expect(within(card).getAllByText('—').length).toBeGreaterThan(0);
  });
});

// ===========================================================================
// ComplianceBuckets
// ===========================================================================

describe('ComplianceBuckets', () => {
  it('renders the empty-state message when compliance_buckets is an empty array', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ compliance_buckets: [] }));
    await mountPage();
    expect(screen.getByText(/No trades reviewed against a rule yet/)).toBeInTheDocument();
  });

  // Guardrail #11 / mutation candidate: the empty gate keys on `scored === 0`
  // (a reduce over trade_count), NOT `rows.length === 0`. A fixture with all
  // four documented buckets present but every trade_count: 0 must STILL show
  // the empty message, not a populated table of zeros -- these two
  // conditions coincide for `rows: []` but diverge here, which is exactly
  // the case a naive fixture (using only `rows: []`) would fail to catch.
  it('renders the empty-state message even with all 4 buckets present when every trade_count is 0 (scored===0, not rows.length===0)', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        compliance_buckets: [
          complianceRow({ compliance: '100%', trade_count: 0 }),
          complianceRow({ compliance: '80-99%', trade_count: 0 }),
          complianceRow({ compliance: '50-79%', trade_count: 0 }),
          complianceRow({ compliance: '<50%', trade_count: 0 }),
        ],
      })
    );
    await mountPage();
    expect(screen.getByText(/No trades reviewed against a rule yet/)).toBeInTheDocument();
    expect(screen.queryByText('100%')).not.toBeInTheDocument();
  });

  it.each<[number | null, number, string]>([
    [null, 5, '—'],
    [1.5, 0, '—'],
    [1.5, 10, '+1.50R (n=10)'],
  ])('avg_r=%s, r_sample=%s -> "%s" (both null and zero-sample reasons render a dash; a real value carries its sample size)', async (avg_r, r_sample, expected) => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        compliance_buckets: [complianceRow({ avg_r, r_sample, trade_count: 5 })],
      })
    );
    await mountPage();
    const card = sectionHeading('Does Overall Compliance Pay?');
    expect(within(card).getByText(expected)).toBeInTheDocument();
  });
});

// ===========================================================================
// MistakeBreakdown
// ===========================================================================

describe('MistakeBreakdown', () => {
  it('renders the empty-state message when mistake_breakdown is empty', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ mistake_breakdown: [] }));
    await mountPage();
    expect(screen.getByText(/No tagged mistakes yet/)).toBeInTheDocument();
  });

  it('colors a positive total_r/avg_r win and a negative one loss', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        mistake_breakdown: [
          mistakeRow({ mistake: 'FOMO', total_r: -2.4, avg_r: -0.8 }),
          mistakeRow({ mistake: 'Patience', total_r: 1.2, avg_r: 0.6 }),
        ],
      })
    );
    await mountPage();
    const card = sectionHeading('Performance by Mistake');
    expect(within(card).getByText('-2.40R')).toHaveClass('text-loss');
    expect(within(card).getByText('-0.80R')).toHaveClass('text-loss');
    expect(within(card).getByText('1.20R')).toHaveClass('text-win');
    expect(within(card).getByText('0.60R')).toHaveClass('text-win');
  });

  it('discloses an unscored trade instead of silently dropping it from the count', async () => {
    // Issue #6 of the calculation audit: tagging a mistake on 3 trades where
    // 1 lacks a stop used to render "2" with nothing hinting a third
    // instance existed. trade_count (3) now includes it; this asserts the
    // disclosure text is actually on screen, not just present in the data.
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        mistake_breakdown: [
          mistakeRow({ mistake: 'Oversized', trade_count: 3, scored: 2, unscored: 1 }),
        ],
      })
    );
    await mountPage();
    const card = sectionHeading('Performance by Mistake');
    expect(within(card).getByText('3')).toBeInTheDocument();
    expect(within(card).getByText('(1 unscored)')).toBeInTheDocument();
  });

  it('does not show an unscored note when every tagged trade was scoreable', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        mistake_breakdown: [mistakeRow({ mistake: 'FOMO', unscored: 0 })],
      })
    );
    await mountPage();
    const card = sectionHeading('Performance by Mistake');
    expect(within(card).queryByText(/unscored/)).not.toBeInTheDocument();
  });

  it('shows a dash rather than crashing when a mistake has nothing scoreable at all', async () => {
    // scored=0 -> avg_r and win_rate_pct are null, not a number to format.
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        mistake_breakdown: [
          mistakeRow({
            mistake: 'No Plan',
            trade_count: 2,
            scored: 0,
            unscored: 2,
            total_r: 0,
            avg_r: null,
            win_rate_pct: null,
          }),
        ],
      })
    );
    await mountPage();
    const card = sectionHeading('Performance by Mistake');
    expect(within(card).getByText('No Plan')).toBeInTheDocument();
    expect(within(card).getByText('(2 unscored)')).toBeInTheDocument();
    // Two dashes: one for Avg R, one for Win %.
    expect(within(card).getAllByText('—')).toHaveLength(2);
  });
});

// ===========================================================================
// queue query states
// ===========================================================================

describe('queue query states', () => {
  it('shows a loading spinner while the queue is pending', () => {
    mocked.getPendingPositions.mockReturnValue(new Promise(() => {}));
    render(<AnalyticsPage />, { wrapper });
    expect(screen.getByText('Loading queue…')).toBeInTheDocument();
  });

  it('shows the Error instance message when the queue fetch rejects with an Error', async () => {
    mocked.getPendingPositions.mockRejectedValue(new Error('queue offline'));
    render(<AnalyticsPage />, { wrapper });
    expect(await screen.findByText('queue offline')).toBeInTheDocument();
  });

  it('falls back to a generic message when the queue fetch rejects with a non-Error', async () => {
    mocked.getPendingPositions.mockImplementation(() => Promise.reject({ code: 1 }));
    render(<AnalyticsPage />, { wrapper });
    expect(await screen.findByText('Failed to load queue.')).toBeInTheDocument();
  });

  it('shows the "every trade reviewed" empty state when the queue is empty', async () => {
    mocked.getPendingPositions.mockResolvedValue([]);
    await mountPage();
    expect(screen.getByText('Every trade reviewed')).toBeInTheDocument();
  });

  it('renders one clickable row per queued position, and opens the drawer on click', async () => {
    mocked.getPendingPositions.mockResolvedValue([
      position({ id: 'pos-1', symbol: 'AAPL' }),
      position({ id: 'pos-2', symbol: 'MSFT' }),
      position({ id: 'pos-3', symbol: 'TSLA' }),
    ]);
    await mountPage();
    expect(screen.getByText('AAPL')).toBeInTheDocument();
    expect(screen.getByText('MSFT')).toBeInTheDocument();
    expect(screen.getByText('TSLA')).toBeInTheDocument();

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('MSFT').closest('button') as HTMLElement);

    const drawer = await screen.findByRole('dialog');
    expect(within(drawer).getByText('Review MSFT')).toBeInTheDocument();
  });

  // Finding #7 (page.tsx 786): the count badge is gated on `!isPending`, not
  // on success -- during an error, isPending is false and `queue` falls back
  // to `[]` (line 651), so a "0" badge sits right next to the error text.
  // Pinned as current (misleading) behaviour, not treated as a bug to fix.
  it('shows a misleading "0" badge alongside the error message on queue error (Finding #7)', async () => {
    mocked.getPendingPositions.mockRejectedValue(new Error('queue offline'));
    render(<AnalyticsPage />, { wrapper });
    await screen.findByText('queue offline');
    const heading = screen.getByText('PENDING REVIEW QUEUE');
    const header = heading.parentElement as HTMLElement;
    expect(within(header).getByText('0')).toBeInTheDocument();
  });

  it('shows no badge while the queue is pending', () => {
    mocked.getPendingPositions.mockReturnValue(new Promise(() => {}));
    render(<AnalyticsPage />, { wrapper });
    const heading = screen.getByText('PENDING REVIEW QUEUE');
    const header = heading.parentElement as HTMLElement;
    expect(within(header).queryByText('0')).not.toBeInTheDocument();
  });

  it('shows the correct count badge once the queue loads', async () => {
    mocked.getPendingPositions.mockResolvedValue([
      position({ id: 'pos-1' }),
      position({ id: 'pos-2' }),
      position({ id: 'pos-3' }),
    ]);
    await mountPage();
    const heading = screen.getByText('PENDING REVIEW QUEUE');
    const header = heading.parentElement as HTMLElement;
    expect(within(header).getByText('3')).toBeInTheDocument();
  });
});

// ===========================================================================
// ReviewDrawer (mounted via AnalyticsPage)
// ===========================================================================

describe('ReviewDrawer (mounted via AnalyticsPage)', () => {
  async function openDrawerFor(pos: Position) {
    mocked.getPendingPositions.mockResolvedValue([pos]);
    await mountPage();
    fireEvent.click(screen.getByText(pos.symbol).closest('button') as HTMLElement);
    return screen.findByRole('dialog');
  }

  it('is not in the DOM at all when nothing is selected', async () => {
    mocked.getPendingPositions.mockResolvedValue([]);
    await mountPage();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(document.querySelector('[aria-label="Close"]')).not.toBeInTheDocument();
  });

  it('seeds notes from position.notes and tags from position.mistakes when opened', async () => {
    const pos = position({ notes: 'Chased the breakout', mistakes: ['FOMO', 'Chased'] });
    const drawer = await openDrawerFor(pos);

    expect(within(drawer).getByPlaceholderText(/What was the setup/)).toHaveValue(
      'Chased the breakout'
    );
    expect(within(drawer).getByRole('button', { name: 'FOMO' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
    expect(within(drawer).getByRole('button', { name: 'Chased' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
    expect(within(drawer).getByRole('button', { name: 'Oversized' })).toHaveAttribute(
      'aria-pressed',
      'false'
    );
  });

  it('seeds an empty textarea and no tags when position.notes/mistakes are null/empty', async () => {
    const pos = position({ notes: null, mistakes: [] });
    const drawer = await openDrawerFor(pos);
    expect(within(drawer).getByPlaceholderText(/What was the setup/)).toHaveValue('');
    expect(within(drawer).queryByText(/tags? selected/)).not.toBeInTheDocument();
  });

  // Finding #10 (page.tsx 32, 481, 489, 564-589; Review C / business-logic
  // review): the module comment claims "Free text is also allowed," but the
  // drawer's only tag-editing surface is a fixed MISTAKE_TAGS chip row --
  // there is no text input anywhere in this component. `tags` is seeded
  // straight from `position.mistakes ?? []` with no filtering against
  // MISTAKE_TAGS, so a mistake string outside the fixed 8 (legacy data, a
  // future backend feature, a direct API write) is loaded into state but
  // renders no chip -- `active = tags.includes(tag)` only ever tests
  // membership of the 8 known strings, so nothing lights up for it -- yet
  // the "{tags.length} tag(s) selected" caption still counts it, and Save
  // resubmits it verbatim since nothing in the UI can ever remove it.
  // Pinned here, not fixed, since this is a genuine UI gap (either the
  // comment is wrong and should be deleted, or a free-text input is
  // genuinely missing) rather than a test bug.
  it('a mistake tag outside MISTAKE_TAGS is counted in the caption but has no chip to show or remove it (Finding #10 -- invisible-but-counted foreign tag)', async () => {
    const pos = position({ mistakes: ['Revenge Trading'] });
    const drawer = await openDrawerFor(pos);

    // No chip lights up for it -- none of the 8 fixed buttons match.
    MISTAKE_TAGS_FOR_TEST.forEach((tag) => {
      expect(within(drawer).getByRole('button', { name: tag })).toHaveAttribute(
        'aria-pressed',
        'false'
      );
    });
    // Yet the count caption still includes it.
    expect(within(drawer).getByText('1 tag selected')).toBeInTheDocument();
    // And there is no text input anywhere in the drawer to see, edit, or
    // remove it -- confirming the module's "Free text is also allowed"
    // comment is not backed by any control here.
    expect(drawer.querySelector('input[type="text"]')).toBeNull();

    fireEvent.click(within(drawer).getByRole('button', { name: 'Mark Reviewed' }));
    await waitFor(() =>
      expect(mocked.updatePositionReview).toHaveBeenCalledWith(
        pos.id,
        expect.objectContaining({ mistakes: ['Revenge Trading'] })
      )
    );
  });

  it('toggleTag adds an unselected tag and removes a selected one', async () => {
    const pos = position({ mistakes: [] });
    const drawer = await openDrawerFor(pos);

    const fomoButton = within(drawer).getByRole('button', { name: 'FOMO' });
    fireEvent.click(fomoButton);
    expect(fomoButton).toHaveAttribute('aria-pressed', 'true');
    expect(within(drawer).getByText('1 tag selected')).toBeInTheDocument();

    fireEvent.click(fomoButton);
    expect(fomoButton).toHaveAttribute('aria-pressed', 'false');
    expect(within(drawer).queryByText(/tags? selected/)).not.toBeInTheDocument();
  });

  it('shows the plural "N tags selected" caption for more than one tag', async () => {
    const pos = position({ mistakes: [] });
    const drawer = await openDrawerFor(pos);
    fireEvent.click(within(drawer).getByRole('button', { name: 'FOMO' }));
    fireEvent.click(within(drawer).getByRole('button', { name: 'Chased' }));
    expect(within(drawer).getByText('2 tags selected')).toBeInTheDocument();
  });

  it('Save calls the mutation with exactly { id, payload: { notes, mistakes, mark_reviewed: false } }', async () => {
    const pos = position({ id: 'pos-42', notes: '', mistakes: [] });
    const drawer = await openDrawerFor(pos);

    fireEvent.change(within(drawer).getByPlaceholderText(/What was the setup/), {
      target: { value: 'Entered too early' },
    });
    fireEvent.click(within(drawer).getByRole('button', { name: 'Early Liquidation' }));
    fireEvent.click(within(drawer).getByRole('button', { name: 'Mark Reviewed' }));

    await waitFor(() => {
      expect(mocked.updatePositionReview).toHaveBeenCalledWith('pos-42', {
        notes: 'Entered too early',
        mistakes: ['Early Liquidation'],
        mark_reviewed: false,
      });
    });
  });

  // Finding #8 (page.tsx 505; PositionReviewPayload doc comment,
  // src/types/api.ts 217-223): mark_reviewed is hard-coded false BY DESIGN,
  // so review_status is untouched -- Save closes the drawer but never
  // removes the position from the queue. Confirmed intentional by the
  // payload's own doc comment, but easy to misread as a bug, so it is
  // pinned explicitly rather than assumed.
  it('Save succeeds, closes the drawer, but the position stays in the queue (Finding #8 -- mark_reviewed:false is by design)', async () => {
    const pos = position({ id: 'pos-7', symbol: 'NFLX' });
    const drawer = await openDrawerFor(pos);

    fireEvent.click(within(drawer).getByRole('button', { name: 'Mark Reviewed' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByText('NFLX')).toBeInTheDocument();
  });

  it('Save error keeps the drawer open and displays the error message', async () => {
    mocked.updatePositionReview.mockRejectedValue(new Error('save failed'));
    const pos = position();
    const drawer = await openDrawerFor(pos);

    fireEvent.click(within(drawer).getByRole('button', { name: 'Mark Reviewed' }));

    expect(await within(drawer).findByText('save failed')).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('disables the textarea, tag chips, Cancel, and shows "Saving…" while the mutation is pending', async () => {
    let resolveSave!: (value: Position) => void;
    mocked.updatePositionReview.mockReturnValue(
      new Promise<Position>((resolve) => {
        resolveSave = resolve;
      })
    );
    const pos = position();
    const drawer = await openDrawerFor(pos);

    fireEvent.click(within(drawer).getByRole('button', { name: 'Mark Reviewed' }));

    await waitFor(() => expect(within(drawer).getByText('Saving…')).toBeInTheDocument());
    expect(within(drawer).getByPlaceholderText(/What was the setup/)).toBeDisabled();
    expect(within(drawer).getByRole('button', { name: 'FOMO' })).toBeDisabled();
    expect(within(drawer).getByRole('button', { name: 'Cancel' })).toBeDisabled();

    resolveSave(position());
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('backdrop click while saving is a no-op (does not close the drawer)', async () => {
    let resolveSave!: (value: Position) => void;
    mocked.updatePositionReview.mockReturnValue(
      new Promise<Position>((resolve) => {
        resolveSave = resolve;
      })
    );
    const pos = position();
    const drawer = await openDrawerFor(pos);

    fireEvent.click(within(drawer).getByRole('button', { name: 'Mark Reviewed' }));
    await waitFor(() => expect(within(drawer).getByText('Saving…')).toBeInTheDocument());

    const backdrop = drawer.parentElement?.querySelector('.absolute.inset-0') as HTMLElement;
    fireEvent.click(backdrop);
    expect(screen.getByRole('dialog')).toBeInTheDocument();

    resolveSave(position());
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  // Guardrail #4/#10: the header X button relies on the native `disabled`
  // attribute rather than an `isSaving` conditional, and jsdom (matching
  // real browsers) does not dispatch `click` on a disabled button at all --
  // so this must be tested separately from the backdrop-click guard above,
  // which uses a live conditional and would pass or fail for a different
  // reason entirely.
  it('the header X button is disabled while saving, and jsdom refuses to click it', async () => {
    let resolveSave!: (value: Position) => void;
    mocked.updatePositionReview.mockReturnValue(
      new Promise<Position>((resolve) => {
        resolveSave = resolve;
      })
    );
    const pos = position();
    const drawer = await openDrawerFor(pos);

    fireEvent.click(within(drawer).getByRole('button', { name: 'Mark Reviewed' }));
    await waitFor(() => expect(within(drawer).getByText('Saving…')).toBeInTheDocument());

    const closeButton = within(drawer).getByLabelText('Close');
    expect(closeButton).toBeDisabled();
    fireEvent.click(closeButton);
    // Still open: jsdom does not fire click on a disabled native button.
    expect(screen.getByRole('dialog')).toBeInTheDocument();

    resolveSave(position());
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('reopening on a different position resets notes/tags/error to the new position, discarding leftover state', async () => {
    const posA = position({ id: 'pos-a', symbol: 'AAPL', notes: 'Notes for A', mistakes: ['FOMO'] });
    const posB = position({ id: 'pos-b', symbol: 'MSFT', notes: 'Notes for B', mistakes: [] });
    mocked.getPendingPositions.mockResolvedValue([posA, posB]);
    await mountPage();

    fireEvent.click(screen.getByText('AAPL').closest('button') as HTMLElement);
    let drawer = await screen.findByRole('dialog');
    expect(within(drawer).getByPlaceholderText(/What was the setup/)).toHaveValue('Notes for A');
    // Add an extra, unsaved tag to prove it does not leak into the next open.
    fireEvent.click(within(drawer).getByRole('button', { name: 'Hesitated' }));
    fireEvent.click(within(drawer).getByLabelText('Close'));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    fireEvent.click(screen.getByText('MSFT').closest('button') as HTMLElement);
    drawer = await screen.findByRole('dialog');
    expect(within(drawer).getByPlaceholderText(/What was the setup/)).toHaveValue('Notes for B');
    expect(within(drawer).getByRole('button', { name: 'FOMO' })).toHaveAttribute('aria-pressed', 'false');
    expect(within(drawer).getByRole('button', { name: 'Hesitated' })).toHaveAttribute(
      'aria-pressed',
      'false'
    );
    expect(within(drawer).queryByText(/tags? selected/)).not.toBeInTheDocument();
  });

  it('reopening clears a previous error left over from a failed save', async () => {
    mocked.updatePositionReview.mockRejectedValueOnce(new Error('save failed'));
    const posA = position({ id: 'pos-a', symbol: 'AAPL' });
    const posB = position({ id: 'pos-b', symbol: 'MSFT' });
    mocked.getPendingPositions.mockResolvedValue([posA, posB]);
    await mountPage();

    fireEvent.click(screen.getByText('AAPL').closest('button') as HTMLElement);
    let drawer = await screen.findByRole('dialog');
    fireEvent.click(within(drawer).getByRole('button', { name: 'Mark Reviewed' }));
    expect(await within(drawer).findByText('save failed')).toBeInTheDocument();

    fireEvent.click(within(drawer).getByLabelText('Close'));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    fireEvent.click(screen.getByText('MSFT').closest('button') as HTMLElement);
    drawer = await screen.findByRole('dialog');
    expect(within(drawer).queryByText('save failed')).not.toBeInTheDocument();
  });
});

// ===========================================================================
// timeframe / toolbar interplay (TimeframeToolbar mocked)
// ===========================================================================

describe('timeframe / toolbar interplay (TimeframeToolbar mocked)', () => {
  it('passes window=undefined to the toolbar while metrics are pending (Finding #6)', () => {
    mocked.getAdvancedMetrics.mockReturnValue(new Promise(() => {}));
    render(<AnalyticsPage />, { wrapper });
    expect(screen.getByTestId('toolbar-window')).toHaveTextContent('no-window');
  });

  it('passes window=undefined to the toolbar while metrics are erroring (Finding #6)', async () => {
    mocked.getAdvancedMetrics.mockRejectedValue(new Error('offline'));
    render(<AnalyticsPage />, { wrapper });
    await screen.findByText('offline');
    expect(screen.getByTestId('toolbar-window')).toHaveTextContent('no-window');
  });

  it('passes the real ToolbarWindow to the toolbar once metrics load', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        window: {
          start_date: '2026-01-01',
          end_date: '2026-06-30',
          closed_trades_in_window: 10,
          closed_trades_total: 12,
        },
      })
    );
    await mountPage();
    expect(screen.getByTestId('toolbar-window')).toHaveTextContent(
      '2026-01-01..2026-06-30:10/12'
    );
  });

  it('passes isFetching=false once metrics have loaded and are settled', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics());
    await mountPage();
    expect(screen.getByTestId('toolbar-fetching')).toHaveTextContent('false');
  });

  it('calling onSelect with a new selection triggers getAdvancedMetrics with that selection (guardrail #7)', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics());
    await mountPage();
    mocked.getAdvancedMetrics.mockClear();
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ total_r: 9 }));

    fireEvent.click(screen.getByText('Select YTD'));

    await waitFor(() => expect(mocked.getAdvancedMetrics).toHaveBeenCalled());
    expect(mocked.getAdvancedMetrics.mock.calls[0][0]).toEqual({
      kind: 'preset',
      preset: 'YTD',
    });
  });

  it('selecting a custom timeframe passes the full custom selection through', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics());
    await mountPage();
    mocked.getAdvancedMetrics.mockClear();
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics());

    fireEvent.click(screen.getByText('Select Custom'));

    await waitFor(() => expect(mocked.getAdvancedMetrics).toHaveBeenCalled());
    expect(mocked.getAdvancedMetrics.mock.calls[0][0]).toEqual({
      kind: 'custom',
      id: 'custom-1',
      start_date: '2026-01-01',
      end_date: '2026-06-30',
    });
  });

  // Guardrail #9 / placeholderData: switching timeframe does NOT clear `m`
  // to undefined while the new query is in flight -- the previous window's
  // KPI values stay visible with isFetching true, rather than flashing a
  // loading state. Pinned as the real behaviour of `placeholderData:
  // (previous) => previous` on useAdvancedMetrics.
  it('keeps the previous window/value on screen (stale-data-stays-visible) while a new selection is loading', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ total_r: 5 }));
    await mountPage();
    expect(kpiValue('Total R')).toHaveTextContent('5.00R');

    let resolveNext!: (value: AdvancedMetrics) => void;
    mocked.getAdvancedMetrics.mockReturnValue(
      new Promise<AdvancedMetrics>((resolve) => {
        resolveNext = resolve;
      })
    );

    fireEvent.click(screen.getByText('Select YTD'));

    // Stale value stays, no loading spinner replaces the KPI strip.
    await waitFor(() => expect(screen.getByTestId('toolbar-fetching')).toHaveTextContent('true'));
    expect(kpiValue('Total R')).toHaveTextContent('5.00R');
    expect(screen.queryByText('Loading metrics…')).not.toBeInTheDocument();

    resolveNext(advancedMetrics({ total_r: 9 }));
    await waitFor(() => expect(kpiValue('Total R')).toHaveTextContent('9.00R'));
  });
});

// ===========================================================================
// cross-cutting query interaction
// ===========================================================================

describe('cross-cutting query interaction', () => {
  it('shows both independent spinners when metrics and queue are simultaneously pending', () => {
    mocked.getAdvancedMetrics.mockReturnValue(new Promise(() => {}));
    mocked.getPendingPositions.mockReturnValue(new Promise(() => {}));
    render(<AnalyticsPage />, { wrapper });

    expect(screen.getByText('Loading metrics…')).toBeInTheDocument();
    expect(screen.getByText('Loading queue…')).toBeInTheDocument();
  });

  it('metrics erroring does not prevent the queue from rendering successfully', async () => {
    mocked.getAdvancedMetrics.mockRejectedValue(new Error('metrics down'));
    mocked.getPendingPositions.mockResolvedValue([position({ symbol: 'AAPL' })]);
    render(<AnalyticsPage />, { wrapper });

    expect(await screen.findByText('metrics down')).toBeInTheDocument();
    // Both queries fire concurrently; the queue's own resolution is a
    // separate, independent promise from the metrics query awaited above, so
    // it needs its own readiness wait rather than a bare getByText riding on
    // the coincidence that both mocks happen to settle in the same
    // microtask batch (Review A / mocking-timing review: verified this test
    // fails, on a "still on Loading queue…" error rather than a timeout,
    // when the queue mock is changed to settle after a delay).
    expect(await screen.findByText('AAPL')).toBeInTheDocument();
  });

  it('queue erroring does not prevent the metrics cards from rendering successfully', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ total_r: 2 }));
    mocked.getPendingPositions.mockRejectedValue(new Error('queue down'));
    render(<AnalyticsPage />, { wrapper });

    expect(await screen.findByText('queue down')).toBeInTheDocument();
    // See the readiness-wait note above: the metrics query is independent of
    // the queue query just awaited, so its own KPI value needs its own wait.
    await waitFor(() => expect(kpiValue('Total R')).toHaveTextContent('2.00R'));
  });

  it('opening the drawer while metrics are erroring and saving successfully does not crash the page', async () => {
    mocked.getAdvancedMetrics.mockRejectedValue(new Error('metrics down'));
    mocked.getPendingPositions.mockResolvedValue([position({ id: 'pos-1', symbol: 'AAPL' })]);
    render(<AnalyticsPage />, { wrapper });

    await screen.findByText('metrics down');
    // The queue's resolution is independent of the metrics error just
    // awaited -- findByText (not getByText) waits on its own readiness
    // signal before the click (see readiness-wait note above).
    const aaplRow = await screen.findByText('AAPL');
    fireEvent.click(aaplRow.closest('button') as HTMLElement);
    const drawer = await screen.findByRole('dialog');

    fireEvent.click(within(drawer).getByRole('button', { name: 'Mark Reviewed' }));

    // Save success invalidates advancedMetrics too (useReviewPosition's
    // onSuccess), triggering a refetch of an already-erroring query -- must
    // not throw.
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByText('metrics down')).toBeInTheDocument();
  });
});

// ===========================================================================
// Remaining branch coverage: defensive `?? fallback`s, singular-count edges,
// sign-dependent tone/glyph branches not otherwise exercised above.
// ===========================================================================

describe('additional branch coverage', () => {
  // Every sub-array on AdvancedMetrics is typed as always-present, but each
  // reader in page.tsx defends with `?? []` / `?? {}` anyway. The API mock
  // is not type-checked against the query's declared shape, so a field can
  // be omitted here to exercise that defensive branch directly.
  it('RDistribution falls back to {} when r_distribution is missing from the payload', async () => {
    const metrics = advancedMetrics();
    delete (metrics as Partial<AdvancedMetrics>).r_distribution;
    mocked.getAdvancedMetrics.mockResolvedValue(metrics);
    await mountPage();
    const card = sectionHeading('R-Distribution');
    expect(card.querySelector('.h-full')).not.toBeInTheDocument();
  });

  it('StrategyBreakdownChart falls back to [] when strategy_breakdown is missing from the payload', async () => {
    const metrics = advancedMetrics();
    delete (metrics as Partial<AdvancedMetrics>).strategy_breakdown;
    mocked.getAdvancedMetrics.mockResolvedValue(metrics);
    await mountPage();
    expect(screen.getByText(/No scored trades yet/)).toBeInTheDocument();
  });

  it('DisciplineBreakdown falls back to [] when discipline_breakdown is missing from the payload', async () => {
    const metrics = advancedMetrics();
    delete (metrics as Partial<AdvancedMetrics>).discipline_breakdown;
    mocked.getAdvancedMetrics.mockResolvedValue(metrics);
    await mountPage();
    expect(screen.getByText(/No discipline answers yet/)).toBeInTheDocument();
  });

  it('ComplianceBuckets falls back to [] when compliance_buckets is missing from the payload', async () => {
    const metrics = advancedMetrics();
    delete (metrics as Partial<AdvancedMetrics>).compliance_buckets;
    mocked.getAdvancedMetrics.mockResolvedValue(metrics);
    await mountPage();
    expect(screen.getByText(/No trades reviewed against a rule yet/)).toBeInTheDocument();
  });

  it('MistakeBreakdown falls back to [] when mistake_breakdown is missing from the payload', async () => {
    const metrics = advancedMetrics();
    delete (metrics as Partial<AdvancedMetrics>).mistake_breakdown;
    mocked.getAdvancedMetrics.mockResolvedValue(metrics);
    await mountPage();
    expect(screen.getByText(/No tagged mistakes yet/)).toBeInTheDocument();
  });

  it('ReviewDrawer falls back to [] when position.mistakes is missing entirely', async () => {
    const pos = position();
    delete (pos as Partial<Position>).mistakes;
    mocked.getPendingPositions.mockResolvedValue([pos]);
    await mountPage();
    fireEvent.click(screen.getByText(pos.symbol).closest('button') as HTMLElement);
    const drawer = await screen.findByRole('dialog');
    expect(within(drawer).getByRole('button', { name: 'FOMO' })).toHaveAttribute(
      'aria-pressed',
      'false'
    );
    expect(within(drawer).queryByText(/tags? selected/)).not.toBeInTheDocument();
  });

  it('singularizes the strategy tooltip\'s "scored trade" clause when scored === 1', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        strategy_breakdown: [
          strategyRow({ strategy: 'Solo', total_r: 1, scored: 1, unscored: 0, avg_r: null, win_rate_pct: null }),
        ],
      })
    );
    await mountPage();
    const card = sectionHeading('Which Strategies Are Working');
    const bar = card.querySelector('.absolute.inset-y-1') as HTMLElement;
    expect(bar.title).toBe('Solo: +1.00R over 1 scored trade');
  });

  it('singularizes the coverage footnote when the total unscored count across rows is exactly 1', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        strategy_breakdown: [
          strategyRow({ strategy: 'A', unscored: 1 }),
          strategyRow({ strategy: 'B', unscored: 0 }),
        ],
      })
    );
    await mountPage();
    expect(screen.getByText(/1 trade could not/)).toBeInTheDocument();
    expect(screen.queryByText(/1 trades could not/)).not.toBeInTheDocument();
  });

  it('ComplianceBuckets Win % column renders a dash for a null win_rate_pct', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        compliance_buckets: [complianceRow({ trade_count: 5, win_rate_pct: null })],
      })
    );
    await mountPage();
    const card = sectionHeading('Does Overall Compliance Pay?');
    expect(within(card).getByText('—')).toBeInTheDocument();
  });

  it('ComplianceBuckets Avg R column omits the "+" sign for a negative avg_r', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(
      advancedMetrics({
        compliance_buckets: [complianceRow({ trade_count: 5, avg_r: -1.25, r_sample: 5 })],
      })
    );
    await mountPage();
    const card = sectionHeading('Does Overall Compliance Pay?');
    expect(within(card).getByText('-1.25R (n=5)')).toBeInTheDocument();
    expect(within(card).queryByText('+-1.25R (n=5)')).not.toBeInTheDocument();
  });

  it('backdrop click while NOT saving closes the drawer (the live half of the isSaving guard)', async () => {
    const pos = position();
    mocked.getPendingPositions.mockResolvedValue([pos]);
    await mountPage();
    fireEvent.click(screen.getByText(pos.symbol).closest('button') as HTMLElement);
    const drawer = await screen.findByRole('dialog');

    const backdrop = drawer.parentElement?.querySelector('.absolute.inset-0') as HTMLElement;
    fireEvent.click(backdrop);

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('the drawer header shows text-loss and no "+" sign for a negative realized_pnl', async () => {
    const pos = position({ realized_pnl: -125.5 });
    mocked.getPendingPositions.mockResolvedValue([pos]);
    await mountPage();
    fireEvent.click(screen.getByText(pos.symbol).closest('button') as HTMLElement);
    const drawer = await screen.findByRole('dialog');

    const pnlSpan = within(drawer).getByText('-125.50');
    expect(pnlSpan).toHaveClass('text-loss');
  });

  it('a queued position with a negative realized_pnl renders text-loss and no "+" sign in the queue row', async () => {
    mocked.getPendingPositions.mockResolvedValue([
      position({ id: 'pos-1', symbol: 'AAPL', realized_pnl: -75 }),
    ]);
    await mountPage();
    const pnlSpan = screen.getByText('-75.00');
    expect(pnlSpan).toHaveClass('text-loss');
  });

  it('Expectancy KPI gets the loss tone when expectancy_r is negative', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ expectancy_r: -0.4 }));
    await mountPage();
    expect(kpiValue('Expectancy')).toHaveClass('text-loss');
  });

  it('Expectancy KPI gets the win tone when expectancy_r is a non-negative real number', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ expectancy_r: 0.4 }));
    await mountPage();
    expect(kpiValue('Expectancy')).toHaveClass('text-win');
  });

  it('Avg Slippage KPI gets the win tone when avg_slippage is zero or negative (better than planned)', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(advancedMetrics({ avg_slippage: 0, slippage_sample: 3 }));
    await mountPage();
    expect(kpiValue('Avg Slippage')).toHaveClass('text-win');
  });

  // page.tsx ~709-777: `metricsQuery.isPending ? ... : metricsQuery.isError
  // ? ... : m ? ... : null`. The final `: null` arm requires the query to
  // be neither pending nor erroring while `m` (metricsQuery.data) is still
  // falsy.
  //
  // An earlier draft of this suite claimed that arm was unreachable through
  // any mocked queryFn resolution, reasoning that the only falsy value a
  // mock can resolve to is `undefined`, and TanStack Query throws on
  // `data === undefined` (converting it into `isError`, confirmed below).
  // That reasoning was wrong (caught in adversarial review, Review B /
  // coverage-honesty pass): TanStack's guard
  // (`query-core/src/query.ts`: `if (data === undefined) { ... throw ... }`)
  // is a strict `=== undefined` check, not a falsiness check, so `null` and
  // `0` sail through it and land the query in `isSuccess` with that falsy
  // value as `data` -- exactly the state the `: null` arm needs. The test
  // below exercises that directly, closing the gap to true 100% branch
  // coverage instead of excluding it.
  it('a metrics query that resolves to a falsy-but-defined value (null) renders neither KPI cards nor an error, hitting the final `: null` arm', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(null as unknown as AdvancedMetrics);
    mocked.getPendingPositions.mockResolvedValue([]);
    render(<AnalyticsPage />, { wrapper });

    await waitFor(() => expect(screen.queryByText('Loading metrics…')).not.toBeInTheDocument());
    // Not the error arm (query resolved successfully, however emptily)...
    expect(document.querySelector('.text-loss')).not.toBeInTheDocument();
    // ...and not the truthy-`m` arm either -- no KPI cards, no breakdowns.
    expect(screen.queryByText('Total R')).not.toBeInTheDocument();
    expect(screen.queryByText('Does Following Your Rules Pay?')).not.toBeInTheDocument();
  });

  // Distinct from the above: confirms TanStack Query's own `undefined`
  // guard actually fires and reroutes into `isError`, so that a mock
  // resolving to `undefined` (unlike `null`/`0`) is NOT a second way to
  // reach the `: null` arm -- it is a way to reach the `isError` arm
  // instead. Verified against `node_modules/@tanstack/query-core` behaviour
  // rather than assumed.
  it('an undefined-resolving metrics query lands in isError (via TanStack Query\'s own guard), never in the `m` truthy/falsy branch', async () => {
    mocked.getAdvancedMetrics.mockResolvedValue(undefined as unknown as AdvancedMetrics);
    mocked.getPendingPositions.mockResolvedValue([]);
    render(<AnalyticsPage />, { wrapper });

    await waitFor(() => expect(screen.queryByText('Loading metrics…')).not.toBeInTheDocument());
    // Landed in the isError branch (page.tsx ~700-708), not the final
    // `m ? ... : null` branch -- the error text is whatever message the
    // thrown Error carries, so only its presence is asserted, not its
    // exact wording (that string is TanStack's internal query-hash text,
    // not something this component controls).
    expect(screen.queryByText('Total R')).not.toBeInTheDocument();
    const errorRegion = document.querySelector('.text-loss');
    expect(errorRegion).toBeInTheDocument();
    expect(errorRegion).not.toHaveTextContent('Failed to load metrics.');
  });
});

/**
 * TradeLedger: the journal, grouped by round trip rather than by execution.
 *
 * The API functions it depends on (`getRoundTrips`, `getStrategies`, `getPlans`,
 * `annotateTrade`, `updatePositionReview`, `deleteTrade`, `updateExecution`,
 * `attachPlan`, `detachPlan`, `createManualTrade`) are mocked; the REAL hooks
 * from `useTradeInbox` run against a real `QueryClient` -- same rationale as
 * every other suite in this house style: the hooks are themselves what turns a
 * mocked promise into `isPending`/`onSuccess` behaviour.
 *
 * `usePendingActions` (from `PendingActionProvider`) is stubbed for the exact
 * two reasons already documented in `TradeInboxQueue.test.tsx`: the real
 * provider drives its countdown off `Date.now()` plus a 200ms interval against
 * a 10-second window, and its unmount cleanup fires `commit()` for every
 * still-pending action, which would send a spurious real delete after a test's
 * own assertions already ran. `runScheduledAction` below reproduces what the
 * real provider would have done with a scheduled action -- await `commit()`,
 * then dispatch `onCommitted`/`onError` -- so the mutation underneath still
 * runs for real.
 *
 * `PlanChartView` and `ChartDropzone` (from `@/components/PlanChart`) are
 * stubbed for the same reason `PlanModal.test.tsx` stubs them: their real
 * implementations call `URL.createObjectURL` and, on an actual file,
 * `createImageBitmap` and a canvas 2D context, none of which jsdom
 * implements. `PlanChartManager` (from `@/components/PlanChartManager`) is
 * NOT mocked -- it is its own module now, imports `ChartDropzone`/
 * `PlanChartView` as a normal cross-module import, and so renders for real
 * here against the two stubs above, the same way it does inside
 * `PlanModal.test.tsx`. `ConfirmDialog` and `RepairFillModal` are NOT mocked
 * -- both work fine in jsdom, and `RepairFillModal` is only ever opened by
 * name in the one test that checks the "Add a missing fill" button prefills
 * it; its own deeper behaviour belongs to a suite of its own.
 *
 * `TradeLedger.tsx` has no recharts, canvas, or other jsdom-unimplemented
 * browser API of its own -- verified by reading the file in full, not
 * assumed.
 */

import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  getRoundTrips: vi.fn(),
  getStrategies: vi.fn(),
  getPlans: vi.fn(),
  annotateTrade: vi.fn(),
  updatePositionReview: vi.fn(),
  deleteTrade: vi.fn(),
  updateExecution: vi.fn(),
  attachPlan: vi.fn(),
  detachPlan: vi.fn(),
  createManualTrade: vi.fn(),
  uploadPlanChart: vi.fn(),
  deletePlanChart: vi.fn(),
}));

const pendingActions = vi.hoisted(() => ({
  schedule: vi.fn(),
  pendingIds: new Set<string>(),
}));

vi.mock('@/components/PendingActionProvider', () => ({
  usePendingActions: () => ({
    schedule: pendingActions.schedule,
    isPending: (id: string) => pendingActions.pendingIds.has(id),
    commitNow: vi.fn(),
    undo: vi.fn(),
  }),
}));

const chartDropzoneProps = vi.hoisted(() => ({ current: null as unknown }));

vi.mock('@/components/PlanChart', () => ({
  ChartDropzone: (props: {
    value: unknown;
    onChange: (v: unknown) => void;
    disabled?: boolean;
  }) => {
    chartDropzoneProps.current = props;
    return React.createElement(
      'button',
      { type: 'button', 'data-testid': 'fake-dropzone', disabled: props.disabled },
      'fake dropzone'
    );
  },
  PlanChartView: () => React.createElement('div', { 'data-testid': 'fake-chart-view' }),
}));

import * as api from '@/lib/api';
import { RoundTripChart, TradeLedger } from '@/components/TradeLedger';
import type {
  ExecutionUpdateResult,
  PositionFill,
  RoundTrip,
  Strategy,
  TradeDeleteResult,
  TradePlan,
} from '@/types/api';
import type { PendingAction } from '@/components/PendingActionProvider';

const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function fill(overrides: Partial<PositionFill> = {}): PositionFill {
  return {
    id: 'fill-1',
    trade_id: 'trade-open-1',
    role: 'OPEN',
    quantity: 100,
    price: 150,
    executed_at: '2026-01-05T14:30:00Z',
    ...overrides,
  };
}

/** A plain, closed, unplanned, unreviewed round trip -- the least assuming baseline. */
function roundTrip(overrides: Partial<RoundTrip> = {}): RoundTrip {
  return {
    kind: 'closed',
    key: 'rt-1',
    position_id: 'pos-1',
    plan_trade_id: 'trade-open-1',

    symbol: 'AAPL',
    direction: 'BUY',
    quantity: 100,
    entry_price: 150,
    exit_price: 155,
    entry_time: '2026-01-05T14:30:00Z',
    exit_time: '2026-01-06T15:00:00Z',
    realized_pnl: 500,
    gross_pnl: 510,
    commission: 10,
    execution_count: 2,

    r_multiple: 2.5,
    planned_r_multiple: null,

    strategy_id: null,
    thesis: null,
    planned_entry: null,
    stop_loss: null,
    actual_stop_loss: null,
    target: null,
    risk_percent: null,
    risk_amount: null,
    conviction: null,
    emotional_state: null,

    plan_id: null,
    plan_created_at: null,
    plan_has_chart: false,
    entry_slippage: null,
    has_hand_added_fills: false,

    review_status: 'pending',
    trade_grade: null,
    notes: null,
    mistakes: [],
    review_went_well: null,
    review_went_wrong: null,
    review_lessons: null,
    exit_reason: null,
    ideal_entry: null,
    ideal_stop: null,
    ideal_target: null,
    revised_entry: null,
    revised_stop: null,
    revised_target: null,
    disciplines: [],

    fills: [
      fill({ id: 'fill-1', trade_id: 'trade-open-1', role: 'OPEN', executed_at: '2026-01-05T14:30:00Z' }),
      fill({ id: 'fill-2', trade_id: 'trade-close-1', role: 'CLOSE', price: 155, executed_at: '2026-01-06T15:00:00Z' }),
    ],
    ...overrides,
  };
}

const STRATEGY: Strategy = {
  id: 'strat-1',
  name: 'Momentum Breakout',
  description: null,
  method: 'Momentum',
  entry_criteria: '',
  exit_criteria: '',
  created_at: null,
};

const OTHER_STRATEGY: Strategy = {
  id: 'strat-2',
  name: 'Reversal Fade',
  description: null,
  method: 'Reversal',
  entry_criteria: '',
  exit_criteria: '',
  created_at: null,
};

function plan(overrides: Partial<TradePlan> = {}): TradePlan {
  return {
    id: 'plan-1',
    ticker: 'AAPL',
    direction: 'BUY',
    quantity: 100,
    planned_entry: 150,
    stop_loss: 148,
    take_profit: 156,
    planned_r: 3,
    risk_percent: 1,
    risk_amount: 200,
    strategy_id: null,
    thesis: 'Reclaim of the 50 EMA.',
    status: 'OPEN',
    created_at: null,
    updated_at: null,
    attached_trade_ids: [],
    has_chart: false,
    chart_bytes: null,
    chart_uploaded_at: null,
    disciplines: [],
    candidates: [],
    ...overrides,
  };
}

function tradeDeleteResult(overrides: Partial<TradeDeleteResult> = {}): TradeDeleteResult {
  return {
    deleted_trade_id: 'trade-close-1',
    ticker: 'AAPL',
    positions_removed: 0,
    positions_rebuilt: 0,
    reviews_discarded: 0,
    ...overrides,
  };
}

function executionUpdateResult(overrides: Partial<ExecutionUpdateResult> = {}): ExecutionUpdateResult {
  return {
    trade_id: 'trade-open-1',
    ticker: 'AAPL',
    direction: 'BUY',
    quantity: 100,
    price: 150,
    execution_time: '2026-01-05T14:30:00-05:00',
    edited_at: null,
    broker_original: null,
    positions_removed: 0,
    positions_rebuilt: 0,
    reviews_discarded: 0,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

let client: QueryClient;

function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(QueryClientProvider, { client }, children);
}

/**
 * `getRoundTrips` backs BOTH the main list query and `useOpenRoundTripCount`.
 * The two are distinguished by shape, not by a mock call order: the main
 * query's queryFn always builds a literal object with an explicit `limit` key
 * (even when `kind`/`strategy` are undefined); `useOpenRoundTripCount`'s call
 * is the bare `getRoundTrips({ kind: 'open' })`, which never has `limit` at
 * all.
 */
function mockRoundTripsResponse(mainRows: RoundTrip[], openRows: RoundTrip[] = []) {
  mocked.getRoundTrips.mockImplementation((query: { limit?: number } = {}) => {
    if (!('limit' in query)) return Promise.resolve(openRows);
    return Promise.resolve(mainRows);
  });
}

/** Renders the ledger and waits past the initial loading gate. */
async function mountLedger(rows: RoundTrip[] = [], openRows: RoundTrip[] = []) {
  mockRoundTripsResponse(rows, openRows);
  const utils = render(React.createElement(TradeLedger), { wrapper });
  // The search box renders in every post-loading, non-error state regardless
  // of whether the list is empty or populated -- a stable readiness marker
  // that doesn't depend on which state this particular test wants.
  await screen.findByPlaceholderText('Filter by ticker…');
  return utils;
}

/**
 * Scopes queries to one row, keyed by the symbol in its header button.
 * `.rounded-xl` is the per-row container's own class; the symbol span's
 * nearest such ancestor is that container and nothing else sits between them.
 */
function rowFor(symbol: string): HTMLElement {
  const label = screen.getByText(symbol, { selector: 'span' });
  const row = label.closest('div.rounded-xl');
  if (!row) throw new Error(`No row container found for "${symbol}".`);
  return row as HTMLElement;
}

/**
 * The header button's accessible name is the concatenation of everything in
 * it (symbol, quantity, badges, date) and always STARTS with the symbol.
 * Anchored to `^` so it doesn't also match the row's own
 * "Add a missing {symbol} fill" button once expanded.
 */
function headerButton(symbol: string): HTMLElement {
  return within(rowFor(symbol)).getByRole('button', { name: new RegExp(`^${symbol}`) });
}

async function expandRow(symbol: string) {
  fireEvent.click(headerButton(symbol));
  // Wait for the expanded section's own border to mount before proceeding --
  // SectionHeading's "The Plan" text is present in either branch.
  await within(rowFor(symbol)).findByText(/The Plan/);
}

/**
 * Lets a mutation's own promise chain (and any state update it triggers)
 * settle before an assertion checks a mock was NOT called. `.mutate()` does
 * not invoke the underlying mocked API function synchronously within the
 * same tick -- confirmed empirically via mutation testing, where removing a
 * real early-return guard did not fail the corresponding "is a no-op" test
 * until this flush was added. A macrotask (not just a microtask) is used
 * deliberately, so it runs only after every currently-queued promise chain
 * has fully drained.
 */
async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

/**
 * Reproduces what the REAL PendingActionProvider's `runCommit` does with a
 * scheduled action, since the provider itself is stubbed above.
 */
async function runScheduledAction(action: PendingAction) {
  await act(async () => {
    try {
      const result = await action.commit();
      action.onCommitted?.(result);
    } catch (err) {
      action.onError?.(err instanceof Error ? err : new Error(String(err)));
    }
  });
}

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  pendingActions.pendingIds.clear();

  mockRoundTripsResponse([]);
  mocked.getStrategies.mockResolvedValue([]);
  mocked.getPlans.mockResolvedValue([]);
  // savePlan/saveReview's onSuccess only triggers a notice built from `rt`
  // (closed over), never from the mutation's own resolved value -- any
  // resolvable promise is sufficient here.
  mocked.annotateTrade.mockResolvedValue({});
  mocked.updatePositionReview.mockResolvedValue({});
  mocked.deleteTrade.mockResolvedValue(tradeDeleteResult());
  mocked.updateExecution.mockImplementation((id: string) =>
    Promise.resolve(executionUpdateResult({ trade_id: id }))
  );
  mocked.attachPlan.mockResolvedValue({
    plan_id: 'plan-1',
    ticker: 'AAPL',
    trade_ids: [],
    fields_copied: [],
    status: 'ATTACHED',
  });
  mocked.detachPlan.mockResolvedValue({
    plan_id: 'plan-1',
    ticker: 'AAPL',
    trades_unlinked: 1,
    status: 'OPEN',
  });
  mocked.createManualTrade.mockResolvedValue({});
});

afterEach(() => {
  cleanup();
  client.clear();
});

// ---------------------------------------------------------------------------
// Render states
// ---------------------------------------------------------------------------

describe('render states', () => {
  it('shows a loading state while the initial page is fetching', () => {
    mockRoundTripsResponse([]);
    mocked.getRoundTrips.mockImplementation(() => new Promise(() => {}));
    render(React.createElement(TradeLedger), { wrapper });

    expect(screen.getByText('Loading journal…')).toBeInTheDocument();
  });

  it('renders the error message when the fetch rejects with an Error', async () => {
    mocked.getRoundTrips.mockRejectedValue(new Error('network down'));
    render(React.createElement(TradeLedger), { wrapper });

    expect(await screen.findByText('network down')).toBeInTheDocument();
  });

  it('falls back to a generic message when the fetch rejects with a non-Error (fixed: used to render a blank banner)', async () => {
    // `(error as Error).message` was an unconditional cast, unlike
    // TradeInboxQueue.tsx's `error instanceof Error ? error.message : '...'`.
    // `.message` on a non-Error value is `undefined`, which rendered nothing
    // next to the AlertCircle icon -- a blank banner, not a helpful fallback
    // and not a crash either. Now mirrors the same instanceof guard.
    mocked.getRoundTrips.mockRejectedValue('boom');
    render(React.createElement(TradeLedger), { wrapper });

    expect(await screen.findByText('Failed to load journal.')).toBeInTheDocument();
  });

  it('shows a distinct message for a genuinely empty, unfiltered ledger (fixed: used to always say "match this filter")', async () => {
    await mountLedger([]);

    expect(screen.getByText('Nothing in the journal yet.')).toBeInTheDocument();
  });

  it('still says the filter-specific message once a filter is applied and yields nothing', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      await mountLedger([]);
      fireEvent.change(screen.getByPlaceholderText('Filter by ticker…'), { target: { value: 'ZZZZ' } });

      await act(async () => {
        vi.advanceTimersByTime(250);
      });

      expect(await screen.findByText('No trades match this filter.')).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('renders one row per round trip once loaded', async () => {
    await mountLedger([roundTrip({ key: 'rt-1', symbol: 'AAPL' }), roundTrip({ key: 'rt-2', symbol: 'MSFT' })]);

    expect(screen.getByText('AAPL', { selector: 'span' })).toBeInTheDocument();
    expect(screen.getByText('MSFT', { selector: 'span' })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Filtering, search, and pagination
// ---------------------------------------------------------------------------

describe('filtering, search, and pagination', () => {
  it('requests kind=open/closed/undefined for the three filter tabs', async () => {
    await mountLedger([roundTrip()]);
    mocked.getRoundTrips.mockClear();
    mockRoundTripsResponse([roundTrip()]);

    fireEvent.click(screen.getByRole('button', { name: 'open' }));
    await waitFor(() =>
      expect(mocked.getRoundTrips).toHaveBeenCalledWith(expect.objectContaining({ kind: 'open' }))
    );

    fireEvent.click(screen.getByRole('button', { name: 'closed' }));
    await waitFor(() =>
      expect(mocked.getRoundTrips).toHaveBeenCalledWith(expect.objectContaining({ kind: 'closed' }))
    );

    fireEvent.click(screen.getByRole('button', { name: 'all' }));
    await waitFor(() =>
      expect(mocked.getRoundTrips).toHaveBeenCalledWith(expect.objectContaining({ kind: undefined }))
    );
  });

  it('shows the open count badge only on the open tab, and only when nonzero', async () => {
    await mountLedger([roundTrip()], [roundTrip({ key: 'open-1' }), roundTrip({ key: 'open-2' })]);

    const openTab = screen.getByRole('button', { name: /open/ });
    expect(within(openTab).getByText('2')).toBeInTheDocument();
  });

  it('omits the open count badge when there are no open round trips', async () => {
    await mountLedger([roundTrip()], []);

    const openTab = screen.getByRole('button', { name: /open/ });
    expect(within(openTab).queryByText('0')).toBeNull();
  });

  it('debounces the search box by 250ms before requesting the server', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      await mountLedger([roundTrip()]);
      mocked.getRoundTrips.mockClear();
      mockRoundTripsResponse([roundTrip({ symbol: 'NVDA' })]);

      fireEvent.change(screen.getByPlaceholderText('Filter by ticker…'), { target: { value: 'NVDA' } });
      expect(mocked.getRoundTrips).not.toHaveBeenCalledWith(expect.objectContaining({ search: 'NVDA' }));

      await act(async () => {
        vi.advanceTimersByTime(250);
      });

      await waitFor(() =>
        expect(mocked.getRoundTrips).toHaveBeenCalledWith(expect.objectContaining({ search: 'NVDA' }))
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it('sends the selected strategy id, or omits it for "All Strategies"', async () => {
    mocked.getStrategies.mockResolvedValue([STRATEGY]);
    await mountLedger([roundTrip()]);
    await screen.findByRole('option', { name: 'Momentum Breakout' });
    mocked.getRoundTrips.mockClear();
    mockRoundTripsResponse([roundTrip()]);

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'strat-1' } });
    await waitFor(() =>
      expect(mocked.getRoundTrips).toHaveBeenCalledWith(expect.objectContaining({ strategy: 'strat-1' }))
    );

    mocked.getRoundTrips.mockClear();
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'all' } });
    await waitFor(() =>
      expect(mocked.getRoundTrips).toHaveBeenCalledWith(expect.objectContaining({ strategy: undefined }))
    );
  });

  it('dims the list and marks it aria-busy while swapping to a new filter\'s placeholder data', async () => {
    await mountLedger([roundTrip({ symbol: 'AAPL' })]);
    // Never resolves: keeps isPlaceholderData true after the filter change.
    mocked.getRoundTrips.mockImplementation(() => new Promise(() => {}));

    fireEvent.click(screen.getByRole('button', { name: 'closed' }));

    await waitFor(() => {
      const list = screen.getByText('AAPL', { selector: 'span' }).closest('[aria-busy]');
      expect(list).toHaveAttribute('aria-busy', 'true');
    });
  });

  it('shows "Load older trades" only when hasNextPage, and requests the next offset', async () => {
    const fullPage = Array.from({ length: 50 }, (_, i) =>
      roundTrip({ key: `closed-${i}`, symbol: 'AAPL', kind: 'closed' })
    );
    mocked.getRoundTrips.mockImplementation((query: { offset?: number; limit?: number } = {}) => {
      if (!('limit' in query)) return Promise.resolve([]);
      if ((query.offset ?? 0) > 0) return Promise.resolve([roundTrip({ key: 'page-2', symbol: 'MSFT' })]);
      return Promise.resolve(fullPage);
    });
    render(React.createElement(TradeLedger), { wrapper });
    await screen.findByPlaceholderText('Filter by ticker…');

    const loadMore = await screen.findByRole('button', { name: 'Load older trades' });
    fireEvent.click(loadMore);

    expect(await screen.findByText('MSFT', { selector: 'span' })).toBeInTheDocument();
    expect(mocked.getRoundTrips).toHaveBeenCalledWith(expect.objectContaining({ offset: 50 }));
  });

  it('does not show "Load older trades" when the last page has fewer than 50 closed rows', async () => {
    await mountLedger([roundTrip()]);

    expect(screen.queryByRole('button', { name: 'Load older trades' })).toBeNull();
  });

  it('shows "Loading…" and disables the button while fetching the next page', async () => {
    const fullPage = Array.from({ length: 50 }, (_, i) => roundTrip({ key: `closed-${i}`, symbol: 'AAPL' }));
    mocked.getRoundTrips.mockImplementation((query: { offset?: number; limit?: number } = {}) => {
      if (!('limit' in query)) return Promise.resolve([]);
      if ((query.offset ?? 0) > 0) return new Promise(() => {});
      return Promise.resolve(fullPage);
    });
    render(React.createElement(TradeLedger), { wrapper });
    await screen.findByPlaceholderText('Filter by ticker…');

    fireEvent.click(await screen.findByRole('button', { name: 'Load older trades' }));

    expect(await screen.findByRole('button', { name: 'Loading…' })).toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// Round trip header
// ---------------------------------------------------------------------------

describe('round trip header', () => {
  it.each([
    ['BUY' as const, 'bg-win/10'],
    ['SELL' as const, 'bg-loss/10'],
  ])('colors the direction icon for a %s', async (direction, cls) => {
    await mountLedger([roundTrip({ direction })]);

    // svg[0] is the (always-present, uncolored) chevron; svg[1] is the
    // direction icon, wrapped in the color-bearing div.
    const row = rowFor('AAPL');
    const iconWrap = row.querySelectorAll('svg')[1]?.parentElement;
    expect(iconWrap?.className).toContain(cls);
  });

  it('formats quantity/entry/exit, trimming trailing zeros from the quantity', async () => {
    await mountLedger([roundTrip({ quantity: 0.25, entry_price: 150, exit_price: 155 })]);

    expect(screen.getByText('0.25 @ 150 → 155')).toBeInTheDocument();
  });

  it('omits the exit price entirely for an open round trip', async () => {
    await mountLedger([roundTrip({ kind: 'open', exit_price: null })]);

    expect(screen.getByText('100 @ 150')).toBeInTheDocument();
    expect(screen.queryByText(/→/)).toBeNull();
  });

  it.each([
    ['closed' as const, 'Closed'],
    ['open' as const, 'Open'],
  ])('labels a %s round trip as %s', async (kind, label) => {
    await mountLedger([roundTrip({ kind, exit_price: kind === 'open' ? null : 155 })]);

    expect(within(rowFor('AAPL')).getByText(label)).toBeInTheDocument();
  });

  it('shows a fills-count badge only when execution_count is greater than 1', async () => {
    await mountLedger([roundTrip({ execution_count: 1 })]);
    expect(screen.queryByText(/fills$/)).toBeNull();

    cleanup();
    client.clear();
    await mountLedger([roundTrip({ execution_count: 3 })]);
    expect(screen.getByText('3 fills')).toBeInTheDocument();
  });

  it('renders an RBadge for r_multiple, colored by sign, and omits it entirely when null', async () => {
    await mountLedger([roundTrip({ r_multiple: 2.5 })]);
    expect(screen.getByText('+2.50R')).toHaveClass('text-win');

    cleanup();
    client.clear();
    await mountLedger([roundTrip({ r_multiple: -1.5 })]);
    expect(screen.getByText('-1.50R')).toHaveClass('text-loss');

    cleanup();
    client.clear();
    await mountLedger([roundTrip({ r_multiple: null })]);
    expect(screen.queryByText(/R$/)).toBeNull();
  });

  it.each([
    // No literal `$` here -- unlike PnlBreakdown's `money()` helper, the
    // header's own realized_pnl span is just a bare sign + toFixed(2).
    [0, '+0.00', 'text-win'],
    [-75.5, '-75.50', 'text-loss'],
  ])('formats the header realized_pnl of %i as %s (win boundary includes zero)', async (pnl, text, cls) => {
    await mountLedger([roundTrip({ realized_pnl: pnl })]);

    expect(screen.getByText(text)).toHaveClass(cls);
  });

  it('omits the realized_pnl span entirely when null (an open round trip)', async () => {
    await mountLedger([roundTrip({ kind: 'open', exit_price: null, realized_pnl: null })]);

    expect(screen.queryByText(/^[+-]\$/)).toBeNull();
  });

  it.each([
    [0.01, 1_000_000, '< 0.01'],
    [5, 1000, '0.50'],
    [50, 1000, '5.0'],
  ])('renders the fee-drag tier for commission=%i on capital=%i as "%s"', async (commission, capital, tierText) => {
    // capital = entry_price * quantity; entry_price=100, quantity=capital/100.
    await mountLedger([roundTrip({ entry_price: 100, quantity: capital / 100, commission })]);

    expect(screen.getByText(new RegExp(`\\(${tierText.replace('.', '\\.')}%\\)`))).toBeInTheDocument();
  });

  it('labels a negative commission as a Credit (rebate), and a positive one as a Fee', async () => {
    await mountLedger([roundTrip({ commission: -5 })]);
    expect(screen.getByText(/^Credit:/)).toBeInTheDocument();

    cleanup();
    client.clear();
    await mountLedger([roundTrip({ commission: 5 })]);
    expect(screen.getByText(/^Fee:/)).toBeInTheDocument();
  });

  it('omits the commission badge entirely when commission is null or exactly zero', async () => {
    await mountLedger([roundTrip({ commission: null })]);
    expect(screen.queryByText(/^(Fee|Credit):/)).toBeNull();

    cleanup();
    client.clear();
    await mountLedger([roundTrip({ commission: 0 })]);
    expect(screen.queryByText(/^(Fee|Credit):/)).toBeNull();
  });

  it('shows the strategy badge only once the round trip has a strategy_id resolvable via useStrategies', async () => {
    // Scoped to the row: "Momentum Breakout" ALSO appears as an <option> in
    // the top-level strategy filter dropdown, unrelated to this row's badge.
    mocked.getStrategies.mockResolvedValue([STRATEGY]);
    await mountLedger([roundTrip({ strategy_id: 'strat-1' })]);

    expect(await within(rowFor('AAPL')).findByText('Momentum Breakout')).toBeInTheDocument();
  });

  it('omits the strategy badge when strategy_id points at nothing useStrategies returned', async () => {
    mocked.getStrategies.mockResolvedValue([STRATEGY]);
    await mountLedger([roundTrip({ strategy_id: 'strat-does-not-exist' })]);
    await waitFor(() => expect(mocked.getStrategies).toHaveBeenCalled());

    expect(within(rowFor('AAPL')).queryByText('Momentum Breakout')).toBeNull();
  });

  it('shows the Planned badge only when plan_id is set', async () => {
    await mountLedger([roundTrip({ plan_id: 'plan-1' })]);
    expect(screen.getByText('Planned')).toBeInTheDocument();

    cleanup();
    client.clear();
    await mountLedger([roundTrip({ plan_id: null })]);
    expect(screen.queryByText('Planned')).toBeNull();
  });

  it('shows the Hand-added badge only when has_hand_added_fills is true', async () => {
    await mountLedger([roundTrip({ has_hand_added_fills: true })]);
    expect(screen.getByText('Hand-added')).toBeInTheDocument();

    cleanup();
    client.clear();
    await mountLedger([roundTrip({ has_hand_added_fills: false })]);
    expect(screen.queryByText('Hand-added')).toBeNull();
  });

  it('shows the thesis icon only when thesis is set', async () => {
    await mountLedger([roundTrip({ thesis: 'Reclaim of the 50 EMA.' })]);
    expect(rowFor('AAPL').querySelector('svg.lucide-notebook-pen')).toBeTruthy();

    cleanup();
    client.clear();
    await mountLedger([roundTrip({ thesis: null })]);
    expect(rowFor('AAPL').querySelector('svg.lucide-notebook-pen')).toBeNull();
  });

  it('falls back to entry_time for the trailing date when exit_time is null (an open round trip)', async () => {
    await mountLedger([
      roundTrip({ kind: 'open', exit_price: null, exit_time: null, entry_time: '2026-02-01T10:00:00Z' }),
    ]);

    expect(screen.getByText(/^Feb 01, 2026/)).toBeInTheDocument();
  });

  it('does not crash when entry_price is null despite the type -- capitalCommitted falls back to 0', async () => {
    // Typed as plain `number`, not nullable -- forced via a cast for the same
    // reason TradeInboxQueue.test.tsx casts a malformed field to exercise
    // defensive code the type system otherwise disallows.
    //
    // Only entry_price, not quantity: a null quantity crashes this same
    // header first, in formatQuantity's unguarded `quantity.toFixed(8)`
    // (line 60) -- so `rt.quantity ?? 0` in capitalCommitted can never
    // actually run to its own rescue. That fallback is left undocumented as
    // "confirmed unreachable" for that reason; it is unreachable WITHOUT
    // crashing, which is a different and less reassuring claim.
    await mountLedger([
      roundTrip({ entry_price: null as unknown as number, commission: 5 }),
    ]);

    // capitalCommitted=0 -> feeDragPct=0 (the ">0" guard fails) -> falls into
    // the toFixed(2) tier ("0.00"), not "< 0.01".
    expect(screen.getByText(/\(0\.00%\)/)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Expand / collapse and PnlBreakdown
// ---------------------------------------------------------------------------

describe('expand / collapse', () => {
  it('starts every row collapsed', async () => {
    await mountLedger([roundTrip()]);

    expect(screen.queryByText('The Plan')).toBeNull();
  });

  it('expands a row on click and collapses it again on a second click', async () => {
    await mountLedger([roundTrip()]);

    await expandRow('AAPL');
    expect(screen.getByText(/The Plan/)).toBeInTheDocument();

    fireEvent.click(headerButton('AAPL'));
    expect(screen.queryByText(/The Plan/)).toBeNull();
  });

  it('only ever keeps one row expanded at a time', async () => {
    await mountLedger([
      roundTrip({ key: 'rt-1', symbol: 'AAPL' }),
      roundTrip({ key: 'rt-2', symbol: 'MSFT' }),
    ]);

    await expandRow('AAPL');
    expect(within(rowFor('AAPL')).getByText(/The Plan/)).toBeInTheDocument();

    await expandRow('MSFT');
    expect(within(rowFor('MSFT')).getByText(/The Plan/)).toBeInTheDocument();
    expect(within(rowFor('AAPL')).queryByText(/The Plan/)).toBeNull();
  });

  it('renders Gross/Commission/Net, and a "winner before costs" badge when gross was positive but net was not', async () => {
    await mountLedger([roundTrip({ gross_pnl: 100, commission: 150, realized_pnl: -50 })]);
    await expandRow('AAPL');

    expect(screen.getByText('+$100.00')).toBeInTheDocument();
    expect(screen.getByText('−$150.00')).toBeInTheDocument();
    expect(screen.getByText('−$50.00')).toBeInTheDocument();
    expect(screen.getByText('a winner before costs')).toBeInTheDocument();
  });

  it('renders Rebate (not Commission) in the breakdown for a negative commission', async () => {
    await mountLedger([roundTrip({ gross_pnl: 100, commission: -5, realized_pnl: 105 })]);
    await expandRow('AAPL');

    expect(screen.getByText('Rebate')).toBeInTheDocument();
    expect(screen.queryByText('Commission')).toBeNull();
  });

  it('omits the whole PnlBreakdown when any of gross/commission/realized_pnl is null', async () => {
    await mountLedger([roundTrip({ kind: 'open', exit_price: null, gross_pnl: null, commission: null, realized_pnl: null })]);
    await expandRow('AAPL');

    expect(screen.queryByText(/^Gross/)).toBeNull();
  });

  it('omits the "winner before costs" badge when net is also positive', async () => {
    await mountLedger([roundTrip({ gross_pnl: 100, commission: 10, realized_pnl: 90 })]);
    await expandRow('AAPL');

    expect(screen.queryByText('a winner before costs')).toBeNull();
  });

  it('colors a negative gross_pnl in the breakdown as a loss too', async () => {
    await mountLedger([roundTrip({ gross_pnl: -50, commission: 5, realized_pnl: -55 })]);
    await expandRow('AAPL');

    expect(screen.getByText('−$50.00')).toHaveClass('text-loss/70');
  });
});

// ---------------------------------------------------------------------------
// The Plan section: PlanVsExecution vs AttachPlanPanel, save, attach/detach
// ---------------------------------------------------------------------------

describe('plan section', () => {
  it('shows PlanVsExecution when plan_id is set, and AttachPlanPanel otherwise', async () => {
    await mountLedger([roundTrip({ plan_id: 'plan-1', plan_created_at: '2026-01-05T12:00:00Z' })]);
    await expandRow('AAPL');
    expect(screen.getByText('Planned before entry')).toBeInTheDocument();
    expect(screen.queryByText('No plan attached')).toBeNull();

    cleanup();
    client.clear();
    await mountLedger([roundTrip({ plan_id: null })]);
    await expandRow('AAPL');
    expect(screen.getByText('No plan attached')).toBeInTheDocument();
    expect(screen.queryByText('Planned before entry')).toBeNull();
  });

  it('titles the plan section "as recorded" only when a plan is attached', async () => {
    await mountLedger([roundTrip({ plan_id: 'plan-1' })]);
    await expandRow('AAPL');
    expect(screen.getByText('The Plan · as recorded')).toBeInTheDocument();

    cleanup();
    client.clear();
    await mountLedger([roundTrip({ plan_id: null })]);
    await expandRow('AAPL');
    expect(screen.getByText('The Plan')).toBeInTheDocument();
    expect(screen.queryByText('The Plan · as recorded')).toBeNull();
  });

  describe('PlanVsExecution', () => {
    it('shows the plan-written timestamp only when plan_created_at is set', async () => {
      await mountLedger([roundTrip({ plan_id: 'plan-1', plan_created_at: '2026-01-05T12:00:00Z' })]);
      await expandRow('AAPL');

      expect(screen.getByText(/^written /)).toBeInTheDocument();
    });

    it('shows planned vs. actual entry, and a read-only actual-entry field', async () => {
      await mountLedger([roundTrip({ plan_id: 'plan-1', planned_entry: 148, entry_price: 150 })]);
      await expandRow('AAPL');

      expect(screen.getByText('148.00')).toBeInTheDocument();
      expect(screen.getByText('150.00')).toBeInTheDocument();
      expect(screen.getByLabelText('Actual entry', { exact: false })).toHaveAttribute('readonly');
    });

    it.each([
      [0.5, '+0.5', 'better'],
      [-0.25, '-0.25', 'worse'],
    ])('signs and trims slippage of %d as %s (%s)', async (slip, text, word) => {
      await mountLedger([roundTrip({ plan_id: 'plan-1', entry_slippage: slip })]);
      await expandRow('AAPL');

      expect(screen.getByText(text)).toBeInTheDocument();
      expect(screen.getByText(word)).toBeInTheDocument();
    });

    it('omits the slippage row entirely when entry_slippage is null', async () => {
      await mountLedger([roundTrip({ plan_id: 'plan-1', entry_slippage: null })]);
      await expandRow('AAPL');

      expect(screen.queryByText('Slippage')).toBeNull();
    });

    it('shows "still open" for a null r_multiple, and a signed value otherwise', async () => {
      await mountLedger([roundTrip({ plan_id: 'plan-1', r_multiple: null, kind: 'open', exit_price: null })]);
      await expandRow('AAPL');
      expect(screen.getByText('still open')).toBeInTheDocument();

      cleanup();
      client.clear();
      await mountLedger([roundTrip({ plan_id: 'plan-1', r_multiple: 1.5 })]);
      await expandRow('AAPL');
      // Scoped: the header's own RBadge shows the identical "+1.50R" text.
      const realisedRow = screen.getByText('Realised R').closest('div') as HTMLElement;
      expect(within(realisedRow).getByText('+1.50R')).toBeInTheDocument();
    });

    it('formats a sub-$1 price to 4 decimal places instead of 2', async () => {
      await mountLedger([roundTrip({ plan_id: 'plan-1', planned_entry: 0.5 })]);
      await expandRow('AAPL');

      expect(screen.getByText('0.5000')).toBeInTheDocument();
    });

    it('shows a real planned_r_multiple instead of the dash', async () => {
      await mountLedger([roundTrip({ plan_id: 'plan-1', planned_r_multiple: 3 })]);
      await expandRow('AAPL');

      const plannedRow = screen.getByText('Planned R').closest('div') as HTMLElement;
      expect(within(plannedRow).getByText('3.00R')).toBeInTheDocument();
    });

    it('colors a negative realised R as a loss, with no leading plus sign', async () => {
      await mountLedger([roundTrip({ plan_id: 'plan-1', plan_trade_id: 'trade-open-1', r_multiple: -1.25 })]);
      await expandRow('AAPL');

      const realisedRow = screen.getByText('Realised R').closest('div') as HTMLElement;
      const value = within(realisedRow).getByText('-1.25R');
      expect(value).toHaveClass('text-loss');
      // The bottom-of-section summary line repeats the same figure, in the
      // same color, independently of PlanVsExecution's own copy.
      const summary = screen.getByText('Realised').closest('div') as HTMLElement;
      expect(within(summary).getByText('-1.25R')).toHaveClass('text-loss');
    });

    it('shows the stored chart when plan_has_chart is true, the dropzone otherwise', async () => {
      // Not "no section at all" when there is no chart yet -- that was the
      // bug this replaced. An attached plan with nothing to show still gets
      // a way to add one, since it is otherwise permanently unreachable: the
      // Plan modal's edit mode only ever queries OPEN plans, and an attached
      // plan can never be OPEN again short of unlinking it.
      await mountLedger([roundTrip({ plan_id: 'plan-1', plan_has_chart: true })]);
      await expandRow('AAPL');

      expect(screen.getByTestId('fake-chart-view')).toBeInTheDocument();
      expect(screen.queryByTestId('fake-dropzone')).toBeNull();

      cleanup();
      client.clear();
      await mountLedger([roundTrip({ plan_id: 'plan-1', plan_has_chart: false })]);
      await expandRow('AAPL');
      expect(screen.queryByTestId('fake-chart-view')).toBeNull();
      expect(screen.getByTestId('fake-dropzone')).toBeInTheDocument();
    });

    it('uploads a chart for a plan that had none, keyed by its symbol', async () => {
      mocked.uploadPlanChart.mockResolvedValue({} as TradePlan);
      await mountLedger([
        roundTrip({ plan_id: 'plan-3', symbol: 'NVDA', plan_has_chart: false }),
      ]);
      await expandRow('NVDA');

      const onChange = (chartDropzoneProps.current as { onChange: (v: unknown) => void })
        .onChange;
      act(() => {
        onChange({
          blob: new Blob(['fake'], { type: 'image/webp' }),
          mime: 'image/webp',
          width: 10,
          height: 10,
          encodedBytes: 4,
          originalBytes: 4,
          lossless: true,
        });
      });

      fireEvent.click(await screen.findByRole('button', { name: 'Attach chart' }));

      await waitFor(() => expect(mocked.uploadPlanChart).toHaveBeenCalled());
      const [planId, , filename] = mocked.uploadPlanChart.mock.calls[0];
      expect(planId).toBe('plan-3');
      expect(filename).toBe('NVDA-chart.webp');

      // The Replace/Remove pair replaces the dropzone once the upload
      // resolves -- RoundTripChart's own local override, updating without
      // waiting on a refetch.
      expect(await screen.findByRole('button', { name: 'Replace' })).toBeInTheDocument();
    });
  });

  describe('RoundTripChart', () => {
    it('seeds a fresh override from the prop rather than resyncing an effect', () => {
      const { rerender } = render(
        <RoundTripChart planId="plan-a" ticker="AAPL" initialHasChart={true} />,
        { wrapper }
      );
      expect(screen.getByTestId('fake-chart-view')).toBeInTheDocument();

      // Same instance, same key: React re-renders in place rather than
      // remounting, so a prop change alone must NOT retroactively override
      // whatever this instance's own state already committed to -- there is
      // deliberately no effect watching initialHasChart for exactly that
      // reason. Confirmed here by changing it to false and expecting no
      // change: the seed is read once, on mount, not resynced.
      rerender(<RoundTripChart planId="plan-a" ticker="AAPL" initialHasChart={false} />);
      expect(screen.getByTestId('fake-chart-view')).toBeInTheDocument();
    });

    it('discards a stale override when a different plan is keyed in', () => {
      // The property key={rt.plan_id} exists to guarantee, in TradeLedger:
      // unlinking a plan and later attaching a DIFFERENT one to the same row
      // must not carry the old plan's chart-status override into the new
      // plan's. Simulated here the same way TradeLedger's own key does it --
      // a real React key change forces an unmount and a fresh mount, not a
      // prop update on the same instance.
      const { rerender } = render(
        <RoundTripChart key="plan-a" planId="plan-a" ticker="AAPL" initialHasChart={true} />,
        { wrapper }
      );
      expect(screen.getByTestId('fake-chart-view')).toBeInTheDocument();

      rerender(
        <RoundTripChart key="plan-b" planId="plan-b" ticker="MSFT" initialHasChart={false} />
      );

      // plan-b's own seed (false), not plan-a's stale true.
      expect(screen.queryByTestId('fake-chart-view')).toBeNull();
      expect(screen.getByTestId('fake-dropzone')).toBeInTheDocument();
    });
  });

  describe('unlinking a plan', () => {
    it('unlinks by calling detachPlan with the leading trade id argument only', async () => {
      await mountLedger([roundTrip({ plan_id: 'plan-1', plan_trade_id: 'trade-open-1' })]);
      await expandRow('AAPL');

      fireEvent.click(screen.getByRole('button', { name: /Unlink plan/ }));

      // detachPlan is a direct-reference mutationFn -- called as (id, context).
      await waitFor(() => expect(mocked.detachPlan).toHaveBeenCalled());
      expect(mocked.detachPlan.mock.calls[0][0]).toBe('trade-open-1');
    });
  });

  describe('AttachPlanPanel', () => {
    it('renders nothing at all when plan_trade_id is falsy', async () => {
      await mountLedger([roundTrip({ plan_id: null, plan_trade_id: null })]);
      await expandRow('AAPL');

      expect(screen.queryByText('No plan attached')).toBeNull();
    });

    it('filters candidates to matching ticker and direction', async () => {
      mocked.getPlans.mockResolvedValue([
        plan({ id: 'plan-a', ticker: 'AAPL', direction: 'BUY', planned_entry: 149 }),
        plan({ id: 'plan-b', ticker: 'AAPL', direction: 'SELL', planned_entry: 999 }),
        plan({ id: 'plan-c', ticker: 'MSFT', direction: 'BUY', planned_entry: 999 }),
      ]);
      await mountLedger([roundTrip({ plan_id: null, symbol: 'AAPL', direction: 'BUY' })]);
      await expandRow('AAPL');

      expect(await screen.findByText('entry 149.00')).toBeInTheDocument();
      expect(screen.queryByText('entry 999.00')).toBeNull();
    });

    it('shows a plain message with no plan count when there are no open plans at all', async () => {
      mocked.getPlans.mockResolvedValue([]);
      await mountLedger([roundTrip({ plan_id: null, symbol: 'AAPL', direction: 'BUY' })]);
      await expandRow('AAPL');

      await waitFor(() => expect(mocked.getPlans).toHaveBeenCalled());
      expect(screen.getByText('No open plan for AAPL BUY.')).toBeInTheDocument();
    });

    it('agrees the verb with the noun at exactly one non-matching open plan (fixed: used to say "1 open plan exist")', async () => {
      mocked.getPlans.mockResolvedValue([plan({ id: 'plan-x', ticker: 'MSFT', direction: 'BUY' })]);
      await mountLedger([roundTrip({ plan_id: null, symbol: 'AAPL', direction: 'BUY' })]);
      await expandRow('AAPL');

      expect(
        await screen.findByText('No open plan for AAPL BUY (1 open plan exists for other tickers or directions).')
      ).toBeInTheDocument();
    });

    it('pluralizes the count correctly for two or more non-matching open plans', async () => {
      mocked.getPlans.mockResolvedValue([
        plan({ id: 'plan-x', ticker: 'MSFT', direction: 'BUY' }),
        plan({ id: 'plan-y', ticker: 'GOOG', direction: 'BUY' }),
      ]);
      await mountLedger([roundTrip({ plan_id: null, symbol: 'AAPL', direction: 'BUY' })]);
      await expandRow('AAPL');

      expect(
        await screen.findByText('No open plan for AAPL BUY (2 open plans exist for other tickers or directions).')
      ).toBeInTheDocument();
    });

    it('attaches by calling attachPlan with {tradeId, planId} cleanly', async () => {
      mocked.getPlans.mockResolvedValue([plan({ id: 'plan-a', ticker: 'AAPL', direction: 'BUY' })]);
      await mountLedger([roundTrip({ plan_id: null, plan_trade_id: 'trade-open-1', symbol: 'AAPL', direction: 'BUY' })]);
      await expandRow('AAPL');

      fireEvent.click(await screen.findByRole('button', { name: 'Attach' }));

      // attachPlan's mutationFn is a wrapping lambda -- called cleanly.
      await waitFor(() => expect(mocked.attachPlan).toHaveBeenCalledWith('trade-open-1', 'plan-a'));
    });

    it('does not crash while the open-plans query is still pending -- treats it the same as no candidates', async () => {
      mocked.getPlans.mockReturnValue(new Promise(() => {}));
      await mountLedger([roundTrip({ plan_id: null, symbol: 'AAPL', direction: 'BUY' })]);
      await expandRow('AAPL');

      // No "(N open plans exist...)" parenthetical: that requires `plans`
      // itself to be truthy, which it is not while still loading.
      expect(screen.getByText('No open plan for AAPL BUY.')).toBeInTheDocument();
    });

    it('formats a candidate\'s sub-$1 prices to 4 decimal places, and a null one as a dash', async () => {
      mocked.getPlans.mockResolvedValue([
        plan({ id: 'plan-a', ticker: 'AAPL', direction: 'BUY', planned_entry: 0.5, stop_loss: null }),
      ]);
      await mountLedger([roundTrip({ plan_id: null, symbol: 'AAPL', direction: 'BUY' })]);
      await expandRow('AAPL');

      expect(await screen.findByText('entry 0.5000')).toBeInTheDocument();
      expect(screen.getByText('stop —')).toBeInTheDocument();
    });

    it('shows the candidate\'s written timestamp only when created_at is set', async () => {
      mocked.getPlans.mockResolvedValue([
        plan({ id: 'plan-a', ticker: 'AAPL', direction: 'BUY', created_at: '2026-01-04T09:00:00Z' }),
      ]);
      await mountLedger([roundTrip({ plan_id: null, symbol: 'AAPL', direction: 'BUY' })]);
      await expandRow('AAPL');

      expect(await screen.findByText(/^written /)).toBeInTheDocument();
    });
  });

  it('shows only the two built-in strategy options while useStrategies is still pending, without crashing', async () => {
    mocked.getStrategies.mockReturnValue(new Promise(() => {}));
    await mountLedger([roundTrip({ plan_trade_id: 'trade-open-1' })]);
    await expandRow('AAPL');

    const filterSelect = screen.getAllByRole('combobox')[0];
    expect(within(filterSelect).getAllByRole('option')).toHaveLength(2);
    const planSelect = screen.getByLabelText('Strategy');
    expect(within(planSelect).getAllByRole('option')).toHaveLength(1);
  });

  it('dismisses the delete notice via its own Dismiss button', async () => {
    mocked.updateExecution.mockResolvedValueOnce(
      executionUpdateResult({ ticker: 'AAPL', positions_removed: 1, positions_rebuilt: 1, reviews_discarded: 1 })
    );
    await mountLedger([roundTrip({ fills: [fill({ id: 'f1', trade_id: 'trade-open-1' })] })]);
    await expandRow('AAPL');
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByText(/fill corrected/);

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));

    expect(screen.queryByText(/fill corrected/)).toBeNull();
  });

  it('the Unlink guard is a no-op when plan_trade_id is falsy despite a plan being attached', async () => {
    await mountLedger([roundTrip({ plan_id: 'plan-1', plan_trade_id: null })]);
    await expandRow('AAPL');

    fireEvent.click(screen.getByRole('button', { name: /Unlink plan/ }));
    await flush();

    expect(mocked.detachPlan).not.toHaveBeenCalled();
  });

  it('shows the attach error via the same per-row error slot as detach', async () => {
    mocked.getPlans.mockResolvedValue([plan({ id: 'plan-a', ticker: 'AAPL', direction: 'BUY' })]);
    mocked.attachPlan.mockRejectedValueOnce(new Error('attach failed'));
    await mountLedger([roundTrip({ plan_id: null, plan_trade_id: 'trade-open-1', symbol: 'AAPL', direction: 'BUY' })]);
    await expandRow('AAPL');

    fireEvent.click(await screen.findByRole('button', { name: 'Attach' }));

    expect(await screen.findByText('attach failed')).toBeInTheDocument();
  });

  describe('row-scoped errors (fixed: used to leak across rows via a single global planError)', () => {
    it('keeps a detach error on the row it happened to, not on a differently-expanded row', async () => {
      mocked.detachPlan.mockRejectedValueOnce(new Error('detach failed on A'));
      await mountLedger([
        roundTrip({ key: 'rt-1', symbol: 'AAPL', plan_id: 'plan-1', plan_trade_id: 'trade-a' }),
        roundTrip({ key: 'rt-2', symbol: 'MSFT', plan_id: null, plan_trade_id: 'trade-b' }),
      ]);

      await expandRow('AAPL');
      fireEvent.click(screen.getByRole('button', { name: /Unlink plan/ }));
      expect(await screen.findByText('detach failed on A')).toBeInTheDocument();

      // Collapse A, expand B -- rowErrors is keyed by rt.key (like
      // planDrafts/reviewDrafts), so A's error stays exactly on A.
      fireEvent.click(headerButton('AAPL'));
      await expandRow('MSFT');

      expect(screen.queryByText('detach failed on A')).toBeNull();

      // And it is still there if the user goes back to A.
      fireEvent.click(headerButton('MSFT'));
      await expandRow('AAPL');
      expect(screen.getByText('detach failed on A')).toBeInTheDocument();
    });
  });

  describe('savePlan feedback (fixed: used to be silent on both success and failure)', () => {
    it('sends the full annotation payload built from the plan draft', async () => {
      mocked.getStrategies.mockResolvedValue([STRATEGY]);
      await mountLedger([roundTrip({ plan_id: null, plan_trade_id: 'trade-open-1' })]);
      await expandRow('AAPL');
      // Scoped: "Momentum Breakout" is ALSO an option in the top-level
      // strategy filter select.
      await within(rowFor('AAPL')).findByRole('option', { name: 'Momentum Breakout' });

      fireEvent.change(screen.getByLabelText('Strategy'), { target: { value: 'strat-1' } });
      fireEvent.change(screen.getByLabelText('Planned entry'), { target: { value: '148' } });
      fireEvent.change(screen.getByLabelText('State of mind'), { target: { value: '  calm  ' } });
      fireEvent.change(screen.getByLabelText('Why this trade?'), { target: { value: '  reclaim  ' } });

      fireEvent.click(screen.getByRole('button', { name: 'Save plan' }));

      await waitFor(() => expect(mocked.annotateTrade).toHaveBeenCalled());
      expect(mocked.annotateTrade).toHaveBeenCalledWith('trade-open-1', {
        strategy_id: 'strat-1',
        thesis: 'reclaim',
        planned_entry: 148,
        stop_loss: null,
        actual_stop_loss: null,
        target: null,
        risk_percent: null,
        risk_amount: null,
        conviction: null,
        emotional_state: 'calm',
      });
    });

    it('is a no-op when plan_trade_id is falsy', async () => {
      await mountLedger([roundTrip({ plan_trade_id: null })]);
      await expandRow('AAPL');

      fireEvent.click(screen.getByRole('button', { name: 'Save plan' }));
      await flush();

      expect(mocked.annotateTrade).not.toHaveBeenCalled();
    });

    it('shows the error message in this row\'s error slot when the save rejects', async () => {
      mocked.annotateTrade.mockRejectedValueOnce(new Error('server exploded'));
      await mountLedger([roundTrip({ symbol: 'AAPL', plan_trade_id: 'trade-open-1' })]);
      await expandRow('AAPL');

      fireEvent.click(screen.getByRole('button', { name: 'Save plan' }));

      expect(await screen.findByText('server exploded')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Save plan' })).toBeInTheDocument();
    });

    it('reports the exact success notice, naming the round trip\'s symbol', async () => {
      await mountLedger([roundTrip({ symbol: 'AAPL', plan_trade_id: 'trade-open-1' })]);
      await expandRow('AAPL');

      fireEvent.click(screen.getByRole('button', { name: 'Save plan' }));

      expect(await screen.findByText('AAPL: plan saved.')).toBeInTheDocument();
    });

    it('clears a stale error from a previous attempt before a new save starts', async () => {
      mocked.annotateTrade.mockRejectedValueOnce(new Error('first attempt failed'));
      await mountLedger([roundTrip({ symbol: 'AAPL', plan_trade_id: 'trade-open-1' })]);
      await expandRow('AAPL');
      fireEvent.click(screen.getByRole('button', { name: 'Save plan' }));
      await screen.findByText('first attempt failed');

      fireEvent.click(screen.getByRole('button', { name: 'Save plan' }));

      // The stale error is cleared synchronously, before the new mutation's
      // own outcome (a success, here) has a chance to land.
      expect(screen.queryByText('first attempt failed')).toBeNull();
      expect(await screen.findByText('AAPL: plan saved.')).toBeInTheDocument();
    });

    it('disables the button and shows "Saving…" only while its own mutation is pending', async () => {
      mocked.annotateTrade.mockReturnValue(new Promise(() => {}));
      await mountLedger([roundTrip({ plan_trade_id: 'trade-open-1' })]);
      await expandRow('AAPL');

      fireEvent.click(screen.getByRole('button', { name: 'Save plan' }));

      expect(await screen.findByRole('button', { name: 'Saving…' })).toBeDisabled();
    });
  });

  it('omits strategy_id/thesis/emotional_state from the payload when left blank', async () => {
    await mountLedger([roundTrip({ plan_trade_id: 'trade-open-1' })]);
    await expandRow('AAPL');

    fireEvent.click(screen.getByRole('button', { name: 'Save plan' }));

    await waitFor(() => expect(mocked.annotateTrade).toHaveBeenCalled());
    const [, payload] = mocked.annotateTrade.mock.calls[0];
    expect(payload).toMatchObject({ strategy_id: null, thesis: null, emotional_state: null });
  });

  it('sends every remaining plan field: conviction, both stops, target, risk % and $', async () => {
    await mountLedger([roundTrip({ plan_trade_id: 'trade-open-1' })]);
    await expandRow('AAPL');

    fireEvent.change(screen.getByLabelText('Conviction (1–5)', { exact: false }), { target: { value: '4' } });
    fireEvent.change(screen.getByLabelText('Planned stop'), { target: { value: '145' } });
    fireEvent.change(screen.getByLabelText('Actual stop', { exact: false }), { target: { value: '146' } });
    fireEvent.change(screen.getByLabelText('Target'), { target: { value: '160' } });
    fireEvent.change(screen.getByLabelText('Risk %'), { target: { value: '1.5' } });
    fireEvent.change(screen.getByLabelText('Risk $', { exact: false }), { target: { value: '200' } });

    fireEvent.click(screen.getByRole('button', { name: 'Save plan' }));

    await waitFor(() => expect(mocked.annotateTrade).toHaveBeenCalled());
    const [, payload] = mocked.annotateTrade.mock.calls[0];
    expect(payload).toMatchObject({
      conviction: 4,
      stop_loss: 145,
      actual_stop_loss: 146,
      target: 160,
      risk_percent: 1.5,
      risk_amount: 200,
    });
  });
});

// ---------------------------------------------------------------------------
// The Review section
// ---------------------------------------------------------------------------

describe('review section', () => {
  it('shows the Review section only for a closed round trip, else "Still open"', async () => {
    await mountLedger([roundTrip({ kind: 'closed' })]);
    await expandRow('AAPL');
    expect(screen.getByText('The Review')).toBeInTheDocument();
    expect(screen.queryByText(/unlocks once this position is closed/)).toBeNull();

    cleanup();
    client.clear();
    await mountLedger([roundTrip({ kind: 'open', exit_price: null })]);
    await expandRow('AAPL');
    expect(screen.queryByText('The Review')).toBeNull();
    expect(screen.getByText(/unlocks once this position is closed/)).toBeInTheDocument();
  });

  it('shows "Awaiting review" or "Reviewed" depending on review_status', async () => {
    await mountLedger([roundTrip({ review_status: 'pending' })]);
    await expandRow('AAPL');
    expect(screen.getByText('Awaiting review')).toBeInTheDocument();

    cleanup();
    client.clear();
    await mountLedger([roundTrip({ review_status: 'reviewed' })]);
    await expandRow('AAPL');
    expect(screen.getByText('Reviewed')).toBeInTheDocument();
  });

  it('omits the discipline-score badge when disciplines is empty, shows it otherwise', async () => {
    await mountLedger([roundTrip({ disciplines: [] })]);
    await expandRow('AAPL');
    expect(screen.queryByText(/% compliant/)).toBeNull();

    cleanup();
    client.clear();
    await mountLedger([
      roundTrip({
        disciplines: [
          { discipline_id: 'd1', name: 'Rule 1', followed: true },
          { discipline_id: 'd2', name: 'Rule 2', followed: false },
        ],
      }),
    ]);
    await expandRow('AAPL');
    expect(screen.getByText('· 50% compliant')).toHaveAttribute('title', '1 of 2 answered rules followed');
  });

  it('sends the full review payload built from the review draft', async () => {
    await mountLedger([roundTrip({ position_id: 'pos-1' })]);
    await expandRow('AAPL');

    fireEvent.change(screen.getByLabelText('Exit reason'), { target: { value: 'Target hit' } });
    fireEvent.change(screen.getByLabelText('Grade'), { target: { value: 'A' } });
    fireEvent.change(screen.getByLabelText('Ideal entry'), { target: { value: '149' } });
    fireEvent.change(screen.getByLabelText('What went well'), { target: { value: '  clean entry  ' } });

    fireEvent.click(screen.getByRole('button', { name: 'Save review' }));

    await waitFor(() => expect(mocked.updatePositionReview).toHaveBeenCalled());
    expect(mocked.updatePositionReview).toHaveBeenCalledWith('pos-1', {
      exit_reason: 'Target hit',
      review_went_well: 'clean entry',
      review_went_wrong: null,
      review_lessons: null,
      trade_grade: 'A',
      ideal_entry: 149,
      ideal_stop: null,
      ideal_target: null,
      revised_entry: null,
      revised_stop: null,
      revised_target: null,
    });
  });

  it('is a no-op when position_id is falsy', async () => {
    await mountLedger([roundTrip({ position_id: null })]);
    await expandRow('AAPL');

    fireEvent.click(screen.getByRole('button', { name: 'Save review' }));
    await flush();

    expect(mocked.updatePositionReview).not.toHaveBeenCalled();
  });

  it('shows the error message in this row\'s error slot when the review save rejects', async () => {
    mocked.updatePositionReview.mockRejectedValueOnce(new Error('review save failed'));
    await mountLedger([roundTrip({ symbol: 'AAPL', position_id: 'pos-1' })]);
    await expandRow('AAPL');

    fireEvent.click(screen.getByRole('button', { name: 'Save review' }));

    expect(await screen.findByText('review save failed')).toBeInTheDocument();
  });

  it('reports the exact success notice, naming the round trip\'s symbol', async () => {
    await mountLedger([roundTrip({ symbol: 'AAPL', position_id: 'pos-1' })]);
    await expandRow('AAPL');

    fireEvent.click(screen.getByRole('button', { name: 'Save review' }));

    expect(await screen.findByText('AAPL: review saved.')).toBeInTheDocument();
  });

  it('a plan-save error and a review-save error on the same row do not clobber each other\'s section', async () => {
    // Both route through the same per-row error slot -- confirms the LATEST
    // one wins rather than, say, the review error being silently dropped
    // because a plan error was already showing.
    mocked.annotateTrade.mockRejectedValueOnce(new Error('plan save failed'));
    mocked.updatePositionReview.mockRejectedValueOnce(new Error('review save failed'));
    await mountLedger([roundTrip({ symbol: 'AAPL', plan_trade_id: 'trade-open-1', position_id: 'pos-1' })]);
    await expandRow('AAPL');
    fireEvent.click(screen.getByRole('button', { name: 'Save plan' }));
    await screen.findByText('plan save failed');

    fireEvent.click(screen.getByRole('button', { name: 'Save review' }));

    expect(await screen.findByText('review save failed')).toBeInTheDocument();
    expect(screen.queryByText('plan save failed')).toBeNull();
  });

  it('disables the button and shows "Saving…" only while its own mutation is pending', async () => {
    mocked.updatePositionReview.mockReturnValue(new Promise(() => {}));
    await mountLedger([roundTrip({ position_id: 'pos-1' })]);
    await expandRow('AAPL');

    fireEvent.click(screen.getByRole('button', { name: 'Save review' }));

    expect(await screen.findByRole('button', { name: 'Saving…' })).toBeDisabled();
  });

  it('sends every remaining review field: ideal stop/target and all three revised levels, plus wentWrong/lessons', async () => {
    await mountLedger([roundTrip({ position_id: 'pos-1' })]);
    await expandRow('AAPL');

    fireEvent.change(screen.getByLabelText('Ideal stop'), { target: { value: '147' } });
    fireEvent.change(screen.getByLabelText('Ideal target'), { target: { value: '162' } });
    fireEvent.change(screen.getByLabelText('Revised entry'), { target: { value: '149' } });
    fireEvent.change(screen.getByLabelText('Revised stop'), { target: { value: '146' } });
    fireEvent.change(screen.getByLabelText('Revised target'), { target: { value: '164' } });
    fireEvent.change(screen.getByLabelText('What went wrong'), { target: { value: '  chased the entry  ' } });
    fireEvent.change(screen.getByLabelText('What to learn'), { target: { value: '  wait for retest  ' } });

    fireEvent.click(screen.getByRole('button', { name: 'Save review' }));

    await waitFor(() => expect(mocked.updatePositionReview).toHaveBeenCalled());
    const [, payload] = mocked.updatePositionReview.mock.calls[0];
    expect(payload).toMatchObject({
      ideal_stop: 147,
      ideal_target: 162,
      revised_entry: 149,
      revised_stop: 146,
      revised_target: 164,
      review_went_wrong: 'chased the entry',
      review_lessons: 'wait for retest',
    });
  });
});

// ---------------------------------------------------------------------------
// Executions: display, edit-in-place, and delete-with-undo
// ---------------------------------------------------------------------------

describe('executions', () => {
  const RT = roundTrip({
    direction: 'BUY',
    fills: [
      fill({ id: 'fill-open', trade_id: 'trade-open-1', role: 'OPEN', quantity: 100, price: 150, executed_at: '2026-01-05T14:30:00Z' }),
      fill({ id: 'fill-close', trade_id: 'trade-close-1', role: 'CLOSE', quantity: 100, price: 155, executed_at: '2026-01-06T15:00:00Z' }),
    ],
  });

  it('lists every fill with its role, quantity, price, and edit/delete controls', async () => {
    await mountLedger([RT]);
    await expandRow('AAPL');

    const table = screen.getByRole('table');
    expect(within(table).getByText('OPEN')).toHaveClass('text-win');
    expect(within(table).getByText('CLOSE')).toHaveClass('text-loss');
    expect(within(table).getAllByRole('button', { name: 'Edit' })).toHaveLength(2);
    expect(within(table).getAllByRole('button', { name: 'Delete' })).toHaveLength(2);
  });

  it('hides a fill whose delete is inside its undo window', async () => {
    pendingActions.pendingIds.add('trade:trade-close-1');
    await mountLedger([RT]);
    await expandRow('AAPL');

    expect(screen.queryByText('CLOSE')).toBeNull();
    expect(screen.getByText('OPEN')).toBeInTheDocument();
  });

  describe('editing a fill', () => {
    it('seeds the edit form from the fill, deriving direction from role + round-trip direction', async () => {
      await mountLedger([RT]);
      await expandRow('AAPL');
      const editButtons = screen.getAllByRole('button', { name: 'Edit' });

      // Second fill is the CLOSE leg of a BUY round trip -> derived side SELL.
      fireEvent.click(editButtons[1]);

      const row = screen.getByDisplayValue('SELL').closest('tr') as HTMLElement;
      expect(within(row).getByDisplayValue('100')).toBeInTheDocument();
      expect(within(row).getByDisplayValue('155')).toBeInTheDocument();
    });

    it('derives the opposite pair of sides for a SELL round trip\'s open and close fills', async () => {
      const sellRt = roundTrip({
        direction: 'SELL',
        fills: [
          fill({ id: 'f-open', trade_id: 't-open', role: 'OPEN' }),
          fill({ id: 'f-close', trade_id: 't-close', role: 'CLOSE' }),
        ],
      });
      await mountLedger([sellRt]);
      await expandRow('AAPL');
      const editButtons = screen.getAllByRole('button', { name: 'Edit' });

      fireEvent.click(editButtons[0]);
      expect(screen.getByDisplayValue('SELL')).toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: 'Cancel edit' }));

      fireEvent.click(editButtons[1]);
      expect(screen.getByDisplayValue('BUY')).toBeInTheDocument();
    });

    it('does not crash deriving a direction when the round trip\'s own direction is empty despite the type', async () => {
      // TradeSide is typed as 'BUY' | 'SELL', never '' -- forced via a cast to
      // exercise the `|| 'BUY'` fallback the type otherwise disallows.
      const weirdRt = roundTrip({ direction: '' as unknown as RoundTrip['direction'] });
      await mountLedger([weirdRt]);
      await expandRow('AAPL');

      fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]);

      expect(screen.getByDisplayValue('BUY')).toBeInTheDocument();
    });

    it('cancels back to the display row without calling the mutation', async () => {
      await mountLedger([RT]);
      await expandRow('AAPL');
      fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]);

      fireEvent.click(screen.getByRole('button', { name: 'Cancel edit' }));

      expect(screen.getAllByRole('button', { name: 'Edit' })).toHaveLength(2);
      expect(mocked.updateExecution).not.toHaveBeenCalled();
    });

    it('clears any existing delete notice the moment editing starts', async () => {
      mocked.deleteTrade.mockRejectedValueOnce(new Error('a stale notice'));
      await mountLedger([RT]);
      await expandRow('AAPL');
      fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);
      fireEvent.click(await screen.findByRole('button', { name: 'Delete fill' }));
      const action: PendingAction = pendingActions.schedule.mock.calls[0][0];
      await runScheduledAction(action);
      expect(await screen.findByText('a stale notice')).toBeInTheDocument();

      fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]);

      expect(screen.queryByText('a stale notice')).toBeNull();
    });

    it('rejects a zero or negative quantity without calling the mutation', async () => {
      await mountLedger([RT]);
      await expandRow('AAPL');
      fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]);
      const row = screen.getByDisplayValue('BUY').closest('tr') as HTMLElement;
      fireEvent.change(within(row).getByDisplayValue('100'), { target: { value: '0' } });

      fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

      expect(await screen.findByText('Quantity must be greater than zero.')).toBeInTheDocument();
      expect(mocked.updateExecution).not.toHaveBeenCalled();
    });

    it('rejects a zero or negative price without calling the mutation', async () => {
      await mountLedger([RT]);
      await expandRow('AAPL');
      fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]);
      const row = screen.getByDisplayValue('BUY').closest('tr') as HTMLElement;
      fireEvent.change(within(row).getByDisplayValue('150'), { target: { value: '-5' } });

      fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

      expect(await screen.findByText('Price must be greater than zero.')).toBeInTheDocument();
      expect(mocked.updateExecution).not.toHaveBeenCalled();
    });

    it('sends the edited fields, appending :00 to a non-empty datetime', async () => {
      await mountLedger([RT]);
      await expandRow('AAPL');
      fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]);
      const row = screen.getByDisplayValue('BUY').closest('tr') as HTMLElement;
      fireEvent.change(within(row).getByDisplayValue('100'), { target: { value: '120' } });
      fireEvent.change(within(row).getByDisplayValue('BUY'), { target: { value: 'SELL' } });

      fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

      await waitFor(() => expect(mocked.updateExecution).toHaveBeenCalled());
      const [id, payload] = mocked.updateExecution.mock.calls[0];
      expect(id).toBe('trade-open-1');
      expect(payload).toMatchObject({ direction: 'SELL', quantity: 120, price: 150 });
      expect(payload.execution_time).toMatch(/:00$/);
    });

    it('sends execution_time as undefined when the datetime field is cleared', async () => {
      await mountLedger([RT]);
      await expandRow('AAPL');
      fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]);
      const row = screen.getByDisplayValue('BUY').closest('tr') as HTMLElement;
      const datetimeInput = row.querySelector('input[type="datetime-local"]') as HTMLElement;
      fireEvent.change(datetimeInput, { target: { value: '' } });

      fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

      await waitFor(() => expect(mocked.updateExecution).toHaveBeenCalled());
      const [, payload] = mocked.updateExecution.mock.calls[0];
      expect(payload.execution_time).toBeUndefined();
    });

    it.each([
      [
        'the base case',
        executionUpdateResult({ ticker: 'AAPL', positions_removed: 1, positions_rebuilt: 1 }),
        'AAPL: fill corrected. 1 round trip(s) rebuilt as 1.',
      ],
      [
        'a discarded review',
        executionUpdateResult({ ticker: 'AAPL', positions_removed: 1, positions_rebuilt: 1, reviews_discarded: 1 }),
        'AAPL: fill corrected. 1 round trip(s) rebuilt as 1, 1 review(s) discarded.',
      ],
    ])('reports the exact success notice for %s and exits edit mode', async (_label, result, expectedNotice) => {
      mocked.updateExecution.mockResolvedValueOnce(result);
      await mountLedger([RT]);
      await expandRow('AAPL');
      fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]);
      const row = screen.getByDisplayValue('BUY').closest('tr') as HTMLElement;

      fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

      expect(await screen.findByText(expectedNotice)).toBeInTheDocument();
      expect(screen.getAllByRole('button', { name: 'Edit' })).toHaveLength(2);
    });

    it('reports the error message and stays in edit mode when the save rejects', async () => {
      mocked.updateExecution.mockRejectedValueOnce(new Error('execution update failed'));
      await mountLedger([RT]);
      await expandRow('AAPL');
      fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]);
      const row = screen.getByDisplayValue('BUY').closest('tr') as HTMLElement;

      fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

      expect(await screen.findByText('execution update failed')).toBeInTheDocument();
      expect(screen.getByDisplayValue('BUY')).toBeInTheDocument();
    });

    it('disables Save and Cancel, and shows "Saving…", only while its own mutation is pending', async () => {
      mocked.updateExecution.mockReturnValue(new Promise(() => {}));
      await mountLedger([RT]);
      await expandRow('AAPL');
      fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]);
      const row = screen.getByDisplayValue('BUY').closest('tr') as HTMLElement;

      fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

      expect(await within(row).findByRole('button', { name: 'Saving…' })).toBeDisabled();
      expect(within(row).getByRole('button', { name: 'Cancel edit' })).toBeDisabled();
    });
  });

  describe('deleting a fill', () => {
    it('opens the confirm dialog naming the role, quantity, symbol, and price', async () => {
      await mountLedger([RT]);
      await expandRow('AAPL');

      fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);

      expect(await screen.findByRole('alertdialog', { name: 'Delete this execution?' })).toBeInTheDocument();
      const dialog = screen.getByRole('alertdialog');
      expect(within(dialog).getByText(/open 100 AAPL/)).toBeInTheDocument();
      expect(within(dialog).getByText(/@ 150/)).toBeInTheDocument();
    });

    it('warns about rebuild and review loss only for a closed round trip', async () => {
      await mountLedger([roundTrip({ kind: 'closed' })]);
      await expandRow('AAPL');
      fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);
      expect(await screen.findByText(/the review attached to it is discarded/)).toBeInTheDocument();

      cleanup();
      client.clear();
      await mountLedger([roundTrip({ kind: 'open', exit_price: null })]);
      await expandRow('AAPL');
      fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);
      await screen.findByRole('alertdialog');
      expect(screen.queryByText(/the review attached to it is discarded/)).toBeNull();
    });

    it('cancel closes the dialog and schedules nothing', async () => {
      await mountLedger([RT]);
      await expandRow('AAPL');
      fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);
      await screen.findByRole('alertdialog');

      fireEvent.click(screen.getByRole('button', { name: 'Keep it' }));

      expect(screen.queryByRole('alertdialog')).toBeNull();
      expect(pendingActions.schedule).not.toHaveBeenCalled();
    });

    it('schedules the delete keyed by trade id, with role/quantity/price in the detail', async () => {
      await mountLedger([RT]);
      await expandRow('AAPL');
      fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);
      fireEvent.click(await screen.findByRole('button', { name: 'Delete fill' }));

      expect(pendingActions.schedule).toHaveBeenCalledTimes(1);
      const action: PendingAction = pendingActions.schedule.mock.calls[0][0];
      expect(action.id).toBe('trade:trade-open-1');
      expect(action.label).toBe('AAPL fill deleted');
      expect(action.detail).toBe('open 100 @ 150');
    });

    it('reports the outcome even when no review was discarded (fixed: used to report nothing at all)', async () => {
      // onCommitted used to call setDeleteNotice only when reviews_discarded
      // > 0, so two round trips being removed and rebuilt -- a real,
      // non-trivial outcome -- went unreported. It now always reports the
      // outcome, matching the execution-edit success message's own
      // always-notify shape just below.
      mocked.deleteTrade.mockResolvedValueOnce(
        tradeDeleteResult({ positions_removed: 2, positions_rebuilt: 2, reviews_discarded: 0 })
      );
      await mountLedger([RT]);
      await expandRow('AAPL');
      fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);
      fireEvent.click(await screen.findByRole('button', { name: 'Delete fill' }));
      const action: PendingAction = pendingActions.schedule.mock.calls[0][0];

      await runScheduledAction(action);

      expect(
        await screen.findByText('AAPL: 2 round trip(s) removed, 2 rebuilt.')
      ).toBeInTheDocument();
    });

    it('appends the discarded-review count onto the same notice when one was discarded', async () => {
      mocked.deleteTrade.mockResolvedValueOnce(
        tradeDeleteResult({ ticker: 'AAPL', positions_removed: 1, positions_rebuilt: 1, reviews_discarded: 1 })
      );
      await mountLedger([RT]);
      await expandRow('AAPL');
      fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);
      fireEvent.click(await screen.findByRole('button', { name: 'Delete fill' }));
      const action: PendingAction = pendingActions.schedule.mock.calls[0][0];

      await runScheduledAction(action);

      expect(
        await screen.findByText('AAPL: 1 round trip(s) removed, 1 rebuilt, 1 review(s) discarded.')
      ).toBeInTheDocument();
    });

    it('reports the error message when the delete itself fails', async () => {
      mocked.deleteTrade.mockRejectedValueOnce(new Error('delete failed server-side'));
      await mountLedger([RT]);
      await expandRow('AAPL');
      fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);
      fireEvent.click(await screen.findByRole('button', { name: 'Delete fill' }));
      const action: PendingAction = pendingActions.schedule.mock.calls[0][0];

      await runScheduledAction(action);

      expect(await screen.findByText('delete failed server-side')).toBeInTheDocument();
    });
  });

  describe('a fill left mid-edit (fixed: used to silently resume editing across a collapse/re-expand)', () => {
    // editingFillId is a single, ledger-wide value. It used to be left alone
    // by toggleExpanded, so collapsing without saving or canceling kept the
    // edit "open" in state even though the row had visually closed, and it
    // silently resumed on the next expand. An effect keyed off `expanded`
    // now clears it -- the same reset Cancel already performs -- whenever
    // the previously-expanded row stops being the one showing.

    it('does not reappear in edit mode after a collapse/re-expand', async () => {
      await mountLedger([RT]);
      await expandRow('AAPL');
      fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]);
      expect(screen.getByDisplayValue('BUY')).toBeInTheDocument();

      fireEvent.click(headerButton('AAPL'));
      expect(screen.queryByDisplayValue('BUY')).toBeNull();

      await expandRow('AAPL');

      expect(screen.queryByDisplayValue('BUY')).toBeNull();
    });

    it('is also discarded when a different row is expanded instead of collapsing this one first', async () => {
      // Distinct fill ids from AAPL's, so nothing about this row's own
      // render could coincidentally look like a resumed edit and mask a
      // real leak.
      const other = roundTrip({
        key: 'rt-2',
        symbol: 'MSFT',
        plan_trade_id: 'trade-open-2',
        fills: [
          fill({ id: 'fill-3', trade_id: 'trade-open-2', role: 'OPEN', executed_at: '2026-01-07T14:30:00Z' }),
          fill({ id: 'fill-4', trade_id: 'trade-close-2', role: 'CLOSE', price: 160, executed_at: '2026-01-08T15:00:00Z' }),
        ],
      });
      await mountLedger([RT, other]);
      await expandRow('AAPL');
      fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]);
      expect(screen.getByDisplayValue('BUY')).toBeInTheDocument();

      // AAPL is never explicitly closed -- opening MSFT directly is what
      // collapses it.
      await expandRow('MSFT');
      await expandRow('AAPL');

      expect(screen.queryByDisplayValue('BUY')).toBeNull();
    });
  });

  it('opens "Add a missing fill" prefilled with this row\'s symbol, and can be closed again', async () => {
    await mountLedger([roundTrip({ symbol: 'AAPL' })]);
    await expandRow('AAPL');

    fireEvent.click(screen.getByRole('button', { name: 'Add a missing AAPL fill' }));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByDisplayValue('AAPL')).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('dialog')).toBeNull();
  });
});

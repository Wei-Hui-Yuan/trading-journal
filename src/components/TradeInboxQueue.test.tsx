/**
 * TradeInboxQueue: the review checklist that empties one round trip at a
 * time out of the queue.
 *
 * The API functions it depends on (`getPendingPositions`, `getStrategies`,
 * `getDisciplines`, `getPositionFills`, `getPositionDeleteImpact`,
 * `updatePositionReview`, `deletePosition`, `dismissPosition`,
 * `createDiscipline`, `deleteDiscipline`) are mocked; the REAL hooks from
 * `useTradeInbox` run against a real QueryClient, matching the precedent in
 * `PlanModal.test.tsx` -- the hooks are themselves what turns a mocked
 * promise into `isPending`/`onSuccess` behaviour.
 *
 * `usePendingActions` (from `PendingActionProvider`) IS replaced with a
 * stand-in, for two concrete reasons rather than convenience:
 *
 *   1. The real provider drives its countdown off `Date.now()` plus a 200ms
 *      `setInterval` against a 10-second window. Going through it would mean
 *      fake timers on every delete test, for machinery this component does
 *      not own.
 *   2. Its unmount cleanup calls `flush()`, which fires `commit()` for every
 *      still-pending action. RTL's `cleanup()` in `afterEach` would trigger
 *      that on every test that had scheduled a delete, firing a real
 *      `deletePosition` call after the test's own assertions already ran --
 *      bleeding a spurious mutation into whatever runs next.
 *
 * The stand-in makes `schedule` an inspectable `vi.fn()` and backs `isPending`
 * with a plain `Set`. What the *real* provider would have done with a
 * scheduled action -- await `commit()`, then dispatch `onCommitted`/`onError`
 * -- is reproduced by hand in `runScheduledAction` below, so the mutation
 * underneath still runs for real; only the countdown/toast machinery is gone.
 *
 * `ConfirmDialog` and `PositionFills` are NOT mocked -- both work fine in
 * jsdom, and the delete-confirmation contract (disabled while the preflight
 * loads, the label depending on what else would go) is central enough to
 * this component's own logic that faking it away would test very little.
 */

import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  getPendingPositions: vi.fn(),
  getStrategies: vi.fn(),
  getDisciplines: vi.fn(),
  getPositionFills: vi.fn(),
  getPositionDeleteImpact: vi.fn(),
  updatePositionReview: vi.fn(),
  deletePosition: vi.fn(),
  dismissPosition: vi.fn(),
  createDiscipline: vi.fn(),
  deleteDiscipline: vi.fn(),
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

import * as api from '@/lib/api';
import { TradeInboxQueue } from '@/components/TradeInboxQueue';
import type {
  Discipline,
  Position,
  PositionDeleteImpact,
  PositionDeleteResult,
  PositionReviewPayload,
  SharedRoundTrip,
  Strategy,
} from '@/types/api';
import type { PendingAction } from '@/components/PendingActionProvider';

const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

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

function discipline(overrides: Partial<Discipline> = {}): Discipline {
  return {
    id: 'rule-1',
    name: 'A rule',
    strategy_id: null,
    created_at: null,
    ...overrides,
  };
}

function deleteImpact(overrides: Partial<PositionDeleteImpact> = {}): PositionDeleteImpact {
  return {
    position_id: 'pos-1',
    ticker: 'AAPL',
    executions_deleted: 2,
    shared_round_trips: [],
    reviews_at_risk: 0,
    ...overrides,
  };
}

function sharedRoundTrip(overrides: Partial<SharedRoundTrip> = {}): SharedRoundTrip {
  return {
    position_id: 'pos-2',
    symbol: 'MSFT',
    quantity: 50,
    realized_pnl: -100,
    entry_time: '2026-01-04T14:00:00Z',
    exit_time: '2026-01-04T15:00:00Z',
    has_review: false,
    ...overrides,
  };
}

function deleteResult(overrides: Partial<PositionDeleteResult> = {}): PositionDeleteResult {
  return {
    position_id: 'pos-1',
    ticker: 'AAPL',
    executions_deleted: 2,
    positions_rebuilt: 0,
    suppressed_from_future_syncs: 0,
    positions_removed: 0,
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
 * Renders the queue and waits past the initial positions fetch.
 *
 * Waits for EITHER terminal text rather than picking one from
 * `positions.length` up front: a position given at mount can still render as
 * the empty state if its own key is already inside the undo window (see
 * `pendingActions.pendingIds`), so the input array's length is not always
 * what ends up on screen.
 */
async function mountQueue(positions: Position[]) {
  mocked.getPendingPositions.mockResolvedValue(positions);
  const utils = render(React.createElement(TradeInboxQueue), { wrapper });
  await screen.findByText(/All trades reviewed!|awaiting review/);
  return utils;
}

/**
 * Scopes queries to one card, keyed by the ticker in its summary row.
 *
 * `.rounded-lg` is the per-card container's own class (see the source's
 * `<div key={position.id} className="rounded-lg border ...">`); the symbol
 * span's nearest such ancestor is that container and nothing else in the
 * component tree sits between them, so this is a stable enough hook for a
 * card boundary without a dedicated test id.
 */
function cardFor(symbol: string): HTMLElement {
  const label = screen.getByText(symbol, { selector: 'span' });
  const card = label.closest('div.rounded-lg');
  if (!card) throw new Error(`No card container found for "${symbol}".`);
  return card as HTMLElement;
}

const completeReviewButton = (card: HTMLElement) =>
  within(card).getByRole('button', { name: /Complete Review|Saving/ });

/**
 * The whole Manage Rules panel, located via its heading.
 *
 * Needed because a general rule's name renders TWICE while the popover is
 * open: once as this panel's list item, and once as the per-card checklist's
 * checkbox label. An unscoped `getByText(ruleName)` matches both.
 */
function popoverContainer(): HTMLElement {
  const heading = screen.getByText('Discipline Rules');
  const panel = heading.closest('div')?.parentElement;
  if (!panel) throw new Error('Could not find the popover container.');
  return panel as HTMLElement;
}

/**
 * The popover's close (X) button has no accessible name at all -- its only
 * child is an icon, and there is neither an aria-label nor a title. Found
 * structurally instead: it is the one other element in the heading row
 * alongside the "Discipline Rules" text.
 */
function popoverCloseButton(): HTMLElement {
  const header = screen.getByText('Discipline Rules').closest('div') as HTMLElement;
  const button = header.querySelector('button');
  if (!button) throw new Error('Could not find the popover close button.');
  return button as HTMLElement;
}

/** Same situation for a rule row's delete button, scoped by the rule's name. */
function deleteRuleButton(ruleName: string): HTMLElement {
  const row = within(popoverContainer()).getByText(ruleName).closest('div');
  const button = row?.querySelector('button');
  if (!button) throw new Error(`Could not find the delete button for rule "${ruleName}".`);
  return button as HTMLElement;
}

async function openDeleteDialog(card: HTMLElement) {
  fireEvent.click(within(card).getByRole('button', { name: 'Delete Trade' }));
  await screen.findByRole('alertdialog');
}

/**
 * Reproduces what the REAL PendingActionProvider's `runCommit` does with a
 * scheduled action -- await `commit()`, then dispatch `onCommitted` or
 * `onError` -- since the provider itself is stubbed above. The mutation
 * underneath still runs for real.
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

  mocked.getPendingPositions.mockResolvedValue([]);
  mocked.getStrategies.mockResolvedValue([]);
  mocked.getDisciplines.mockResolvedValue([]);
  mocked.getPositionFills.mockResolvedValue([]);
  mocked.getPositionDeleteImpact.mockResolvedValue(deleteImpact());
  mocked.updatePositionReview.mockImplementation((id: string, payload: PositionReviewPayload) =>
    Promise.resolve(position({ id, ...payload } as Partial<Position>))
  );
  mocked.deletePosition.mockResolvedValue(deleteResult());
  mocked.dismissPosition.mockImplementation((id: string) => Promise.resolve(position({ id })));
  mocked.createDiscipline.mockImplementation(
    (payload: { name: string; strategy_id?: string | null }) =>
      Promise.resolve(
        discipline({ id: 'new-rule', name: payload.name, strategy_id: payload.strategy_id ?? null })
      )
  );
  mocked.deleteDiscipline.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  client.clear();
});

// ---------------------------------------------------------------------------
// Render states
// ---------------------------------------------------------------------------

describe('render states', () => {
  it('shows a loading spinner while positions are being fetched', async () => {
    mocked.getPendingPositions.mockReturnValue(new Promise(() => {}));
    render(React.createElement(TradeInboxQueue), { wrapper });

    expect(screen.getByText('Loading positions…')).toBeInTheDocument();
  });

  it('renders the error message when the fetch rejects with an Error', async () => {
    mocked.getPendingPositions.mockRejectedValue(new Error('network down'));
    render(React.createElement(TradeInboxQueue), { wrapper });

    expect(await screen.findByText('network down')).toBeInTheDocument();
  });

  it('falls back to a generic message when the fetch rejects with a non-Error', async () => {
    // The component's own fallback (`error instanceof Error ? ... : 'Failed
    // to load positions.'`) assumes a non-Error rejection is possible. Vitest
    // mocks are not type-checked against the query's declared error type, so
    // this is directly constructible even though a real `getPendingPositions`
    // never throws anything but an Error -- worth pinning either way, since
    // it is the one branch of this fallback that would otherwise never be
    // exercised.
    mocked.getPendingPositions.mockRejectedValue('boom');
    render(React.createElement(TradeInboxQueue), { wrapper });

    expect(await screen.findByText('Failed to load positions.')).toBeInTheDocument();
  });

  it('shows the empty state when there is nothing to review', async () => {
    await mountQueue([]);

    expect(screen.getByText('All trades reviewed!')).toBeInTheDocument();
    expect(screen.getByText('Nothing is waiting in the queue.')).toBeInTheDocument();
  });

  it.each([
    [1, '1 position awaiting review'],
    [2, '2 positions awaiting review'],
  ])('renders one card per position and pluralises the header for %i', async (count, header) => {
    const positions = Array.from({ length: count }, (_, i) =>
      position({ id: `pos-${i}`, symbol: i === 0 ? 'AAPL' : 'MSFT' })
    );
    await mountQueue(positions);

    expect(screen.getByText(header)).toBeInTheDocument();
    expect(screen.getByText('AAPL')).toBeInTheDocument();
    if (count === 2) expect(screen.getByText('MSFT')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Row hiding / undo window
// ---------------------------------------------------------------------------

describe('row hiding while a delete is pending', () => {
  it('filters out a position whose id is inside its undo window', async () => {
    pendingActions.pendingIds.add('position:pos-1');
    await mountQueue([position({ id: 'pos-1', symbol: 'AAPL' }), position({ id: 'pos-2', symbol: 'MSFT' })]);

    expect(screen.queryByText('AAPL')).toBeNull();
    expect(screen.getByText('MSFT')).toBeInTheDocument();
    expect(screen.getByText('1 position awaiting review')).toBeInTheDocument();
  });

  it('shows the empty state once the only position enters its undo window', async () => {
    pendingActions.pendingIds.add('position:pos-1');
    await mountQueue([position({ id: 'pos-1', symbol: 'AAPL' })]);

    expect(screen.getByText('All trades reviewed!')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Card summary
// ---------------------------------------------------------------------------

describe('card summary', () => {
  it.each([
    [0, '+$0.00', 'text-win'],
    [1500, '+$1,500.00', 'text-win'],
    [-50, '-$50.00', 'text-loss'],
  ])('formats a realized P&L of %i as %s (win boundary is inclusive of zero)', async (pnl, text, cls) => {
    await mountQueue([position({ realized_pnl: pnl })]);

    expect(screen.getByText(text)).toHaveClass(cls);
  });

  it('falls back to the raw string for an unparseable entry_time', async () => {
    await mountQueue([position({ entry_time: 'not-a-date' })]);

    expect(screen.getByText(/not-a-date/)).toBeInTheDocument();
  });

  it('flips aria-expanded on the Executions toggle and collapses the previously open card', async () => {
    await mountQueue([
      position({ id: 'pos-1', symbol: 'AAPL' }),
      position({ id: 'pos-2', symbol: 'MSFT' }),
    ]);
    const cardA = cardFor('AAPL');
    const cardB = cardFor('MSFT');
    const toggleA = within(cardA).getByRole('button', { name: 'Executions' });
    const toggleB = within(cardB).getByRole('button', { name: 'Executions' });

    expect(toggleA).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(toggleA);
    expect(toggleA).toHaveAttribute('aria-expanded', 'true');

    fireEvent.click(toggleB);
    expect(toggleB).toHaveAttribute('aria-expanded', 'true');
    expect(toggleA).toHaveAttribute('aria-expanded', 'false');
  });

  it('collapses a card by clicking its own open toggle again', async () => {
    await mountQueue([position({ symbol: 'AAPL' })]);
    const toggle = screen.getByRole('button', { name: 'Executions' });

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
  });
});

// ---------------------------------------------------------------------------
// Drafts
// ---------------------------------------------------------------------------

describe('drafts', () => {
  it('seeds strategy_id from the position; every other field starts blank', async () => {
    mocked.getStrategies.mockResolvedValue([STRATEGY]);
    await mountQueue([position({ strategy_id: 'strat-1' })]);
    // Wait for the real option to exist before trusting the select's value --
    // setting a <select>'s value to one with no matching <option> yet can
    // read back as "" until the option list catches up.
    await screen.findByRole('option', { name: 'Momentum Breakout' });

    expect(screen.getByRole('combobox')).toHaveValue('strat-1');
    expect(screen.getByLabelText('What went well')).toHaveValue('');
    expect(screen.getByLabelText('What went wrong')).toHaveValue('');
    expect(screen.getByLabelText('What to learn')).toHaveValue('');
  });

  it('keeps each card\'s draft independent of the others', async () => {
    await mountQueue([
      position({ id: 'pos-1', symbol: 'AAPL' }),
      position({ id: 'pos-2', symbol: 'MSFT' }),
    ]);
    const cardA = cardFor('AAPL');
    const cardB = cardFor('MSFT');

    fireEvent.change(within(cardA).getByLabelText('What went well'), {
      target: { value: 'clean break' },
    });

    expect(within(cardA).getByLabelText('What went well')).toHaveValue('clean break');
    expect(within(cardB).getByLabelText('What went well')).toHaveValue('');
  });

  it('starts a checkbox ticked when the position already carries a saved answer', async () => {
    const rule = discipline({ id: 'g1', name: 'General Rule', strategy_id: null });
    mocked.getDisciplines.mockResolvedValue([rule]);
    await mountQueue([
      position({ disciplines: [{ discipline_id: 'g1', name: 'General Rule', followed: true }] }),
    ]);

    expect(screen.getByLabelText('General Rule')).toBeChecked();
  });

  it('clears the active grade when the same grade is clicked again', async () => {
    await mountQueue([position()]);
    const gradeB = screen.getByRole('button', { name: 'B' });

    fireEvent.click(gradeB);
    expect(gradeB).toHaveClass('text-win');

    fireEvent.click(gradeB);
    expect(gradeB).not.toHaveClass('text-win');
  });
});

// ---------------------------------------------------------------------------
// Checklist filtering
// ---------------------------------------------------------------------------

describe('checklist filtering', () => {
  it('shows only general rules until a strategy is selected', async () => {
    mocked.getStrategies.mockResolvedValue([STRATEGY]);
    mocked.getDisciplines.mockResolvedValue([
      discipline({ id: 'g1', name: 'General Rule', strategy_id: null }),
      discipline({ id: 's1', name: 'Strategy Rule', strategy_id: 'strat-1' }),
    ]);
    await mountQueue([position()]);

    expect(screen.getByLabelText('General Rule')).toBeInTheDocument();
    expect(screen.queryByLabelText('Strategy Rule')).toBeNull();
  });

  it('surfaces a strategy\'s rules immediately once picked in the dropdown, with no save', async () => {
    // Deliberate: the checklist is filtered by the DRAFT selection, not the
    // position's already-saved strategy_id, so this must happen with no
    // network call in between.
    mocked.getStrategies.mockResolvedValue([STRATEGY]);
    mocked.getDisciplines.mockResolvedValue([
      discipline({ id: 'g1', name: 'General Rule', strategy_id: null }),
      discipline({ id: 's1', name: 'Strategy Rule', strategy_id: 'strat-1' }),
    ]);
    await mountQueue([position()]);
    await screen.findByRole('option', { name: 'Momentum Breakout' });

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'strat-1' } });

    expect(screen.getByLabelText('Strategy Rule')).toBeInTheDocument();
    expect(screen.getByLabelText('General Rule')).toBeInTheDocument();
    expect(mocked.updatePositionReview).not.toHaveBeenCalled();
  });

  it('never shows another strategy\'s rules', async () => {
    mocked.getStrategies.mockResolvedValue([STRATEGY, OTHER_STRATEGY]);
    mocked.getDisciplines.mockResolvedValue([
      discipline({ id: 's1', name: 'Strategy Rule', strategy_id: 'strat-1' }),
      discipline({ id: 's2', name: 'Other Strategy Rule', strategy_id: 'strat-2' }),
    ]);
    await mountQueue([position()]);
    await screen.findByRole('option', { name: 'Momentum Breakout' });

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'strat-1' } });

    expect(screen.getByLabelText('Strategy Rule')).toBeInTheDocument();
    expect(screen.queryByLabelText('Other Strategy Rule')).toBeNull();
  });

  it('says so when there are no discipline rules defined at all', async () => {
    mocked.getDisciplines.mockResolvedValue([]);
    await mountQueue([position()]);

    expect(screen.getByText('No discipline rules defined.')).toBeInTheDocument();
  });

  it('shows a loading placeholder for strategies and rules while their own queries are still in flight', async () => {
    // Every other test in this file mocks getStrategies/getDisciplines with
    // promises that resolve on the next microtask, and by the time
    // `mountQueue` finishes waiting on the positions query both have already
    // settled -- so this in-place loading state, which the component does
    // render, is otherwise never observed.
    mocked.getStrategies.mockReturnValue(new Promise(() => {}));
    mocked.getDisciplines.mockReturnValue(new Promise(() => {}));
    await mountQueue([position()]);

    expect(screen.getByRole('option', { name: 'Loading…' })).toBeInTheDocument();
    expect(screen.getByRole('combobox')).toBeDisabled();
    expect(screen.getByText('Loading rules…')).toBeInTheDocument();
  });

  it('does not crash selecting a strategy while its rules are still loading behind it', async () => {
    // Exercises the OTHER copy of `disciplinesQuery.data ?? []` -- the one
    // inside `checklistForDraft`'s strategy-selected branch, as opposed to
    // `generalDisciplines`'s. Needs strategies resolved (so there is an
    // option to pick) but disciplines still pending at the moment a strategy
    // is chosen, which needs its own promise rather than the shared default.
    mocked.getStrategies.mockResolvedValue([STRATEGY]);
    let resolveDisciplines: (value: Discipline[]) => void = () => {};
    mocked.getDisciplines.mockReturnValue(
      new Promise((resolve) => {
        resolveDisciplines = resolve;
      })
    );
    await mountQueue([position()]);
    await screen.findByRole('option', { name: 'Momentum Breakout' });

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'strat-1' } });
    expect(screen.getByText('Loading rules…')).toBeInTheDocument();

    resolveDisciplines([discipline({ id: 's1', name: 'Strategy Rule', strategy_id: 'strat-1' })]);

    await waitFor(() => expect(screen.getByLabelText('Strategy Rule')).toBeInTheDocument());
  });
});

// ---------------------------------------------------------------------------
// handleSubmit payload
// ---------------------------------------------------------------------------

describe('handleSubmit payload', () => {
  function submitAndCapture() {
    fireEvent.click(screen.getByRole('button', { name: /Complete Review|Saving/ }));
    return waitFor(() => expect(mocked.updatePositionReview).toHaveBeenCalled()).then(
      // `updatePositionReview`'s mutationFn is a lambda -- `({id, payload}) =>
      // updatePositionReview(id, payload)` -- so it is called cleanly as
      // `(id, payload)` with no trailing TanStack context object.
      () => mocked.updatePositionReview.mock.calls[0][1] as PositionReviewPayload
    );
  }

  it('omits the disciplines key entirely when there are no rules at all', async () => {
    mocked.getDisciplines.mockResolvedValue([]);
    await mountQueue([position()]);

    const payload = await submitAndCapture();

    expect(payload).not.toHaveProperty('disciplines');
  });

  it('also omits disciplines when the rules query has not resolved yet at submit time', async () => {
    // Nothing gates the Complete Review button on disciplinesQuery.isPending,
    // so this is reachable through a real (if narrow) timing window: submit
    // fires before the rules list has loaded.
    mocked.getDisciplines.mockReturnValue(new Promise(() => {}));
    await mountQueue([position()]);

    const payload = await submitAndCapture();

    expect(payload).not.toHaveProperty('disciplines');
    expect(payload).not.toHaveProperty('tag_hard_sl');
  });

  it('answers every visible rule, with an untouched rule sent as an explicit false', async () => {
    mocked.getDisciplines.mockResolvedValue([
      discipline({ id: 'r1', name: 'Rule one' }),
      discipline({ id: 'r2', name: 'Rule two' }),
    ]);
    await mountQueue([position()]);
    fireEvent.click(screen.getByLabelText('Rule one'));

    const payload = await submitAndCapture();

    expect(payload.disciplines).toEqual({ r1: true, r2: false });
  });

  it('carries forward a saved answer for a rule the draft never touched', async () => {
    mocked.getDisciplines.mockResolvedValue([discipline({ id: 'g1', name: 'General Rule' })]);
    await mountQueue([
      position({ disciplines: [{ discipline_id: 'g1', name: 'General Rule', followed: true }] }),
    ]);

    const payload = await submitAndCapture();

    expect(payload.disciplines).toEqual({ g1: true });
  });

  it(
    'answers a rule for a strategy that was never selected, and so was never rendered on screen',
    async () => {
      // This is the discrepancy the source's own comment ("every rule ON
      // SCREEN gets an answer... a rule left out entirely stays unanswered")
      // claims does not happen. `handleSubmit` iterates `disciplinesQuery.data`
      // -- ALL disciplines -- rather than the filtered `checklistForDraft` the
      // render actually shows, so a strategy-scoped rule for a strategy this
      // card never selected is submitted as `false` ("did not follow")
      // despite never having appeared as a checkbox. That pollutes compliance
      // analytics with false negatives for rules a trader never saw.
      mocked.getStrategies.mockResolvedValue([STRATEGY]);
      const strategyRule = discipline({ id: 's1', name: 'Strategy Rule', strategy_id: 'strat-1' });
      mocked.getDisciplines.mockResolvedValue([strategyRule]);
      await mountQueue([position({ strategy_id: null })]);

      expect(screen.queryByLabelText('Strategy Rule')).toBeNull();

      const payload = await submitAndCapture();

      expect(payload.disciplines).toEqual({ s1: false });
    }
  );

  it.each([
    ['Hard stop-loss set', 'tag_hard_sl'],
    ['Waited for retest', 'tag_retest'],
    ['Followed the plan', 'tag_plan_compliant'],
  ] as const)('mirrors a ticked "%s" onto the legacy field %s', async (ruleName, tagKey) => {
    mocked.getDisciplines.mockResolvedValue([discipline({ id: 'r1', name: ruleName })]);
    await mountQueue([position()]);
    fireEvent.click(screen.getByLabelText(ruleName));

    const payload = await submitAndCapture();

    expect(payload).toMatchObject({ [tagKey]: true });
  });

  it.each([
    ['Hard stop-loss set', 'tag_hard_sl'],
    ['Waited for retest', 'tag_retest'],
    ['Followed the plan', 'tag_plan_compliant'],
  ] as const)('omits the legacy field %s when no rule is named "%s"', async (ruleName, tagKey) => {
    mocked.getDisciplines.mockResolvedValue([discipline({ id: 'other', name: 'Unrelated rule' })]);
    await mountQueue([position()]);

    const payload = await submitAndCapture();

    expect(payload).not.toHaveProperty(tagKey);
  });

  it('omits strategy_id and trade_grade from the payload when neither is set', async () => {
    await mountQueue([position()]);

    const payload = await submitAndCapture();

    expect(payload).not.toHaveProperty('strategy_id');
    expect(payload).not.toHaveProperty('trade_grade');
  });

  it('includes strategy_id and trade_grade once both are set', async () => {
    mocked.getStrategies.mockResolvedValue([STRATEGY]);
    await mountQueue([position()]);
    await screen.findByRole('option', { name: 'Momentum Breakout' });
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'strat-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'A' }));

    const payload = await submitAndCapture();

    expect(payload.strategy_id).toBe('strat-1');
    expect(payload.trade_grade).toBe('A');
  });

  it.each([
    ['review_went_well', 'What went well'],
    ['review_went_wrong', 'What went wrong'],
    ['review_lessons', 'What to learn'],
  ] as const)('trims real content typed into %s before sending', async (field, label) => {
    await mountQueue([position()]);
    fireEvent.change(screen.getByLabelText(label), {
      target: { value: '  clean break of resistance  ' },
    });

    const payload = await submitAndCapture();

    expect(payload[field]).toBe('clean break of resistance');
  });

  it.each([
    ['review_went_well', 'What went well'],
    ['review_went_wrong', 'What went wrong'],
    ['review_lessons', 'What to learn'],
  ] as const)('trims %s and omits it from the payload when only whitespace was entered', async (field, label) => {
    await mountQueue([position()]);
    fireEvent.change(screen.getByLabelText(label), { target: { value: '   ' } });

    const payload = await submitAndCapture();

    expect(payload).not.toHaveProperty(field);
  });
});

// ---------------------------------------------------------------------------
// Submit lifecycle
// ---------------------------------------------------------------------------

describe('submit lifecycle', () => {
  it('clears that card\'s draft once the review is saved', async () => {
    await mountQueue([position()]);
    fireEvent.change(screen.getByLabelText('What went well'), {
      target: { value: 'nice entry' },
    });

    fireEvent.click(screen.getByRole('button', { name: /Complete Review|Saving/ }));

    await waitFor(() => expect(mocked.updatePositionReview).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByLabelText('What went well')).toHaveValue(''));
  });

  it('renders a failure only on the card that failed, leaving the other clean', async () => {
    mocked.updatePositionReview.mockRejectedValueOnce(new Error('server exploded'));
    await mountQueue([
      position({ id: 'pos-1', symbol: 'AAPL' }),
      position({ id: 'pos-2', symbol: 'MSFT' }),
    ]);
    const cardA = cardFor('AAPL');
    const cardB = cardFor('MSFT');

    fireEvent.click(completeReviewButton(cardA));

    expect(await within(cardA).findByText('server exploded')).toBeInTheDocument();
    expect(within(cardB).queryByText('server exploded')).toBeNull();
  });

  it('disables only the submitting card\'s own controls, not the whole queue', async () => {
    mocked.updatePositionReview.mockReturnValue(new Promise(() => {}));
    await mountQueue([
      position({ id: 'pos-1', symbol: 'AAPL' }),
      position({ id: 'pos-2', symbol: 'MSFT' }),
    ]);
    const cardA = cardFor('AAPL');
    const cardB = cardFor('MSFT');

    fireEvent.click(completeReviewButton(cardA));

    await waitFor(() => expect(completeReviewButton(cardA)).toBeDisabled());
    expect(within(cardA).getByRole('button', { name: 'A' })).toBeDisabled();
    expect(within(cardA).getByLabelText('What went well')).toBeDisabled();
    expect(within(cardB).getByRole('button', { name: 'A' })).not.toBeDisabled();
    expect(within(cardB).getByLabelText('What went well')).not.toBeDisabled();
    expect(completeReviewButton(cardB)).not.toBeDisabled();
  });

  it('shows "Saving…" on the button while its own mutation is in flight', async () => {
    mocked.updatePositionReview.mockReturnValue(new Promise(() => {}));
    await mountQueue([position()]);

    fireEvent.click(screen.getByRole('button', { name: 'Complete Review' }));

    expect(await screen.findByRole('button', { name: 'Saving…' })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Manage Rules popover
//
// Single-position fixtures throughout this block, deliberately. The popover
// state (`isManagingRules`, `newRuleName`, `ruleError`) is declared ONCE at
// component level, but the popover is rendered INSIDE `positions.map()` -- so
// with two or more pending positions, opening it on one card opens it on
// every card, sharing one input and one error across all of them. With a
// single position that ambiguity cannot show up, which is exactly why it is
// tested this way rather than left to be discovered by accident.
// ---------------------------------------------------------------------------

describe('manage rules popover', () => {
  it('opens on click and closes on a second click', async () => {
    await mountQueue([position()]);

    fireEvent.click(screen.getByRole('button', { name: 'Manage Rules' }));
    expect(screen.getByText('Discipline Rules')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Manage Rules' }));
    expect(screen.queryByText('Discipline Rules')).toBeNull();
  });

  it('also closes via its own close button', async () => {
    await mountQueue([position()]);
    fireEvent.click(screen.getByRole('button', { name: 'Manage Rules' }));

    fireEvent.click(popoverCloseButton());

    expect(screen.queryByText('Discipline Rules')).toBeNull();
  });

  it('lists only general rules, never a strategy-scoped one', async () => {
    mocked.getDisciplines.mockResolvedValue([
      discipline({ id: 'g1', name: 'General One', strategy_id: null }),
      discipline({ id: 'g2', name: 'General Two', strategy_id: null }),
      discipline({ id: 's1', name: 'Scoped Rule', strategy_id: 'strat-1' }),
    ]);
    await mountQueue([position()]);

    fireEvent.click(screen.getByRole('button', { name: 'Manage Rules' }));

    const popover = popoverContainer();
    expect(within(popover).getByText('General One')).toBeInTheDocument();
    expect(within(popover).getByText('General Two')).toBeInTheDocument();
    expect(within(popover).queryByText('Scoped Rule')).toBeNull();
  });

  it('disables Add for an empty or whitespace-only name, without calling the API', async () => {
    await mountQueue([position()]);
    fireEvent.click(screen.getByRole('button', { name: 'Manage Rules' }));

    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled();

    fireEvent.change(screen.getByPlaceholderText('New general rule…'), {
      target: { value: '   ' },
    });
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled();
    expect(mocked.createDiscipline).not.toHaveBeenCalled();
  });

  it('trims the name before creating a rule', async () => {
    await mountQueue([position()]);
    fireEvent.click(screen.getByRole('button', { name: 'Manage Rules' }));
    fireEvent.change(screen.getByPlaceholderText('New general rule…'), {
      target: { value: '  Waited for volume  ' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    // `createDiscipline` is passed directly as `mutationFn`, so TanStack Query
    // calls it as `(payload, context)` -- assert on the leading argument only.
    await waitFor(() => expect(mocked.createDiscipline).toHaveBeenCalled());
    expect(mocked.createDiscipline.mock.calls[0][0]).toEqual({ name: 'Waited for volume' });
  });

  it('clears the input once the rule is created', async () => {
    await mountQueue([position()]);
    fireEvent.click(screen.getByRole('button', { name: 'Manage Rules' }));
    const input = screen.getByPlaceholderText('New general rule…');
    fireEvent.change(input, { target: { value: 'New rule' } });

    fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    await waitFor(() => expect(input).toHaveValue(''));
  });

  it('shows the error message when creating a rule fails', async () => {
    mocked.createDiscipline.mockRejectedValueOnce(new Error('name already exists'));
    await mountQueue([position()]);
    fireEvent.click(screen.getByRole('button', { name: 'Manage Rules' }));
    fireEvent.change(screen.getByPlaceholderText('New general rule…'), {
      target: { value: 'Duplicate' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    expect(await screen.findByText('name already exists')).toBeInTheDocument();
  });

  it('deletes a rule by id (leading argument only)', async () => {
    mocked.getDisciplines.mockResolvedValue([discipline({ id: 'g1', name: 'Retire me' })]);
    await mountQueue([position()]);
    fireEvent.click(screen.getByRole('button', { name: 'Manage Rules' }));

    fireEvent.click(deleteRuleButton('Retire me'));

    // `deleteDiscipline` is also a direct-reference mutationFn.
    await waitFor(() => expect(mocked.deleteDiscipline).toHaveBeenCalled());
    expect(mocked.deleteDiscipline.mock.calls[0][0]).toBe('g1');
  });
});

// ---------------------------------------------------------------------------
// Dismiss
// ---------------------------------------------------------------------------

describe('dismiss', () => {
  it('calls dismissPosition with the position id (leading argument only)', async () => {
    await mountQueue([position({ id: 'pos-1' })]);

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));

    // `dismissPosition` is a direct-reference mutationFn -- called as
    // `(id, context)`, so only the leading argument is asserted.
    await waitFor(() => expect(mocked.dismissPosition).toHaveBeenCalled());
    expect(mocked.dismissPosition.mock.calls[0][0]).toBe('pos-1');
  });

  it('reports the exact dismissal notice on success', async () => {
    await mountQueue([position({ symbol: 'AAPL' })]);

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));

    expect(
      await screen.findByText('AAPL dismissed — it keeps its P&L and leaves the queue.')
    ).toBeInTheDocument();
  });

  it('reports the error message on failure', async () => {
    mocked.dismissPosition.mockRejectedValueOnce(new Error('network hiccup'));
    await mountQueue([position()]);

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));

    expect(await screen.findByText('network hiccup')).toBeInTheDocument();
  });

  it('clears the notice via its own dismiss button', async () => {
    await mountQueue([position({ symbol: 'AAPL' })]);
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    await screen.findByText(/dismissed/);

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss message' }));

    expect(screen.queryByText(/dismissed/)).toBeNull();
  });

  it('disables the button while its own dismiss is in flight', async () => {
    mocked.dismissPosition.mockReturnValue(new Promise(() => {}));
    await mountQueue([position()]);
    const button = screen.getByRole('button', { name: 'Dismiss' });

    fireEvent.click(button);

    await waitFor(() => expect(button).toBeDisabled());
  });
});

// ---------------------------------------------------------------------------
// Delete flow
// ---------------------------------------------------------------------------

describe('delete flow', () => {
  it('opens the confirm dialog titled for this position\'s symbol', async () => {
    await mountQueue([position({ symbol: 'AAPL' })]);
    const card = cardFor('AAPL');

    await openDeleteDialog(card);

    expect(
      screen.getByRole('alertdialog', { name: 'Delete this AAPL round trip?' })
    ).toBeInTheDocument();
  });

  it('disables Confirm while the delete-impact preflight is still loading', async () => {
    mocked.getPositionDeleteImpact.mockReturnValue(new Promise(() => {}));
    await mountQueue([position()]);
    const card = cardFor('AAPL');

    await openDeleteDialog(card);

    expect(screen.getByText('Checking what else this would remove…')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete round trip' })).toBeDisabled();
  });

  it.each([
    [0, 'Delete round trip'],
    [1, 'Delete both'],
    // Three extras still says "Delete both" -- the button's label does not
    // scale with the count the way the body text right above it does
    // ("3 other round trips will be deleted too"). Cosmetic, but it is the
    // label on an irreversible action, so pinned rather than left implicit.
    [3, 'Delete both'],
  ])('labels the confirm button for %i shared round trip(s) as "%s"', async (count, label) => {
    mocked.getPositionDeleteImpact.mockResolvedValue(
      deleteImpact({
        shared_round_trips: Array.from({ length: count }, (_, i) =>
          sharedRoundTrip({ position_id: `shared-${i}` })
        ),
      })
    );
    await mountQueue([position()]);
    const card = cardFor('AAPL');

    await openDeleteDialog(card);

    expect(await screen.findByRole('button', { name: label })).toBeInTheDocument();
  });

  it.each([
    [1, 'It shares', '1 other round trip will be deleted too.'],
    [2, 'They share', '2 other round trips will be deleted too.'],
  ])('pluralises the shared-round-trip warning for %i', async (count, pronoun, headline) => {
    mocked.getPositionDeleteImpact.mockResolvedValue(
      deleteImpact({
        shared_round_trips: Array.from({ length: count }, (_, i) =>
          sharedRoundTrip({ position_id: `shared-${i}`, symbol: 'MSFT' })
        ),
      })
    );
    await mountQueue([position()]);
    await openDeleteDialog(cardFor('AAPL'));

    expect(await screen.findByText(headline)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(pronoun))).toBeInTheDocument();
  });

  it('warns when reviews are at risk', async () => {
    mocked.getPositionDeleteImpact.mockResolvedValue(
      deleteImpact({ shared_round_trips: [sharedRoundTrip()], reviews_at_risk: 1 })
    );
    await mountQueue([position()]);
    await openDeleteDialog(cardFor('AAPL'));

    expect(
      await screen.findByText(
        '1 review cannot be rebuilt. Everything else re-matches from the fills; this does not.'
      )
    ).toBeInTheDocument();
  });

  it('pluralises the reviews-at-risk warning for more than one', async () => {
    mocked.getPositionDeleteImpact.mockResolvedValue(
      deleteImpact({
        shared_round_trips: [sharedRoundTrip(), sharedRoundTrip({ position_id: 'shared-2' })],
        reviews_at_risk: 2,
      })
    );
    await mountQueue([position()]);
    await openDeleteDialog(cardFor('AAPL'));

    expect(
      await screen.findByText(
        '2 reviews cannot be rebuilt. Everything else re-matches from the fills; this does not.'
      )
    ).toBeInTheDocument();
  });

  it('shows a winning shared round trip with its own review badge', async () => {
    mocked.getPositionDeleteImpact.mockResolvedValue(
      deleteImpact({
        shared_round_trips: [
          sharedRoundTrip({ symbol: 'MSFT', realized_pnl: 250, has_review: true }),
        ],
      })
    );
    await mountQueue([position()]);
    await openDeleteDialog(cardFor('AAPL'));

    const row = await screen.findByRole('listitem');
    expect(within(row).getByText('+$250.00')).toHaveClass('text-win');
    expect(within(row).getByText('· reviewed')).toBeInTheDocument();
  });

  it('shows a fallback warning when the preflight itself fails', async () => {
    mocked.getPositionDeleteImpact.mockRejectedValueOnce(new Error('impact check failed'));
    await mountQueue([position()]);
    await openDeleteDialog(cardFor('AAPL'));

    expect(
      await screen.findByText(
        'Could not check for shared executions (impact check failed). Deleting may remove more than this round trip.'
      )
    ).toBeInTheDocument();
  });

  it('shows the P&L this delete would remove, with the sign the value carries', async () => {
    // Scoped to the dialog rather than `screen`: the dialog's own P&L text is
    // built from separate `{sign}$` and `{amount}` pieces with no comma
    // formatting, unlike the card's `currency()` helper, and for a value with
    // no thousands separator (as here) the two would otherwise render
    // identical text and collide.
    await mountQueue([position({ realized_pnl: 500 })]);
    await openDeleteDialog(cardFor('AAPL'));
    const dialog = screen.getByRole('alertdialog');

    expect(within(dialog).getByText(/Its P&L of/)).toBeInTheDocument();
    expect(within(dialog).getByText('+$500.00')).toBeInTheDocument();
  });

  it('colors the dialog\'s own P&L paragraph for a loss, with no explicit sign', async () => {
    await mountQueue([position({ realized_pnl: -100 })]);
    await openDeleteDialog(cardFor('AAPL'));
    const dialog = screen.getByRole('alertdialog');

    // Only the win case prepends '+'; a loss's leading '-' comes from
    // toFixed itself, landing after the literal '$' rather than before it.
    const amount = within(dialog).getByText('$-100.00');
    expect(amount).toHaveClass('text-loss');
  });

  it('omits the P&L paragraph when realized_pnl is not present', async () => {
    // Position.realized_pnl is typed as a plain `number`, but the component
    // guards with `!= null` anyway, implying the API can hand back a real
    // position without one. Forced via a cast for the same reason
    // discipline.test.ts casts a malformed `followed` value: to exercise
    // defensive code the type system otherwise rules out.
    await mountQueue([position({ realized_pnl: null as unknown as number })]);
    await openDeleteDialog(cardFor('AAPL'));

    expect(screen.queryByText(/Its P&L of/)).toBeNull();
  });

  it('cancel closes the dialog and schedules nothing', async () => {
    await mountQueue([position()]);
    await openDeleteDialog(cardFor('AAPL'));

    fireEvent.click(screen.getByRole('button', { name: 'Keep it' }));

    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(pendingActions.schedule).not.toHaveBeenCalled();
  });

  it('schedules the delete with the plain label when nothing else is shared', async () => {
    await mountQueue([position({ id: 'pos-1', symbol: 'AAPL', realized_pnl: 500 })]);
    await openDeleteDialog(cardFor('AAPL'));

    fireEvent.click(await screen.findByRole('button', { name: 'Delete round trip' }));

    expect(pendingActions.schedule).toHaveBeenCalledTimes(1);
    const action: PendingAction = pendingActions.schedule.mock.calls[0][0];
    expect(action.id).toBe('position:pos-1');
    expect(action.label).toBe('AAPL round trip deleted');
    expect(action.detail).toBe('P&L of +$500.00 leaves your analytics');
  });

  it('carries no explicit sign in the schedule detail for a losing round trip', async () => {
    // Only the `>= 0` branch prepends '+'; a negative value's own leading '-'
    // (from toFixed) is what the reader actually sees, giving "$-100.00".
    await mountQueue([position({ id: 'pos-1', symbol: 'AAPL', realized_pnl: -100 })]);
    await openDeleteDialog(cardFor('AAPL'));

    fireEvent.click(await screen.findByRole('button', { name: 'Delete round trip' }));

    const action: PendingAction = pendingActions.schedule.mock.calls[0][0];
    expect(action.detail).toBe('P&L of $-100.00 leaves your analytics');
  });

  it('names the extra count in the schedule label when round trips are shared', async () => {
    mocked.getPositionDeleteImpact.mockResolvedValue(
      deleteImpact({ shared_round_trips: [sharedRoundTrip(), sharedRoundTrip({ position_id: 'shared-2' })] })
    );
    await mountQueue([position({ symbol: 'AAPL' })]);
    await openDeleteDialog(cardFor('AAPL'));

    fireEvent.click(await screen.findByRole('button', { name: 'Delete both' }));

    const action: PendingAction = pendingActions.schedule.mock.calls[0][0];
    expect(action.label).toBe('AAPL round trip deleted, with 2 more');
  });

  it('leaves detail undefined when realized_pnl is not present', async () => {
    await mountQueue([position({ realized_pnl: null as unknown as number })]);
    await openDeleteDialog(cardFor('AAPL'));

    fireEvent.click(await screen.findByRole('button', { name: 'Delete round trip' }));

    const action: PendingAction = pendingActions.schedule.mock.calls[0][0];
    expect(action.detail).toBeUndefined();
  });

  it('commits with includeShared=false when nothing else was named', async () => {
    await mountQueue([position({ id: 'pos-1' })]);
    await openDeleteDialog(cardFor('AAPL'));
    fireEvent.click(await screen.findByRole('button', { name: 'Delete round trip' }));
    const action: PendingAction = pendingActions.schedule.mock.calls[0][0];

    await runScheduledAction(action);

    expect(mocked.deletePosition).toHaveBeenCalledWith('pos-1', 'Deleted from the Trade Inbox', false);
  });

  it('commits with includeShared=true once shared round trips were named', async () => {
    mocked.getPositionDeleteImpact.mockResolvedValue(
      deleteImpact({ shared_round_trips: [sharedRoundTrip()] })
    );
    await mountQueue([position({ id: 'pos-1' })]);
    await openDeleteDialog(cardFor('AAPL'));
    fireEvent.click(await screen.findByRole('button', { name: 'Delete both' }));
    const action: PendingAction = pendingActions.schedule.mock.calls[0][0];

    await runScheduledAction(action);

    expect(mocked.deletePosition).toHaveBeenCalledWith('pos-1', 'Deleted from the Trade Inbox', true);
  });

  it.each([
    [
      'the base case',
      deleteResult(),
      'AAPL: removed, 2 execution(s) deleted.',
    ],
    [
      'other round trips removed with it',
      deleteResult({ positions_removed: 2 }),
      'AAPL: removed, 2 execution(s) deleted, 2 other round trip(s) removed with it.',
    ],
    [
      'reviews discarded, nested inside positions_removed',
      deleteResult({ positions_removed: 1, reviews_discarded: 1 }),
      'AAPL: removed, 2 execution(s) deleted, 1 other round trip(s) removed with it and 1 review(s) lost.',
    ],
    [
      'suppressed from future syncs',
      deleteResult({ suppressed_from_future_syncs: 3 }),
      'AAPL: removed, 2 execution(s) deleted, 3 suppressed from future syncs.',
    ],
  ])('reports the notice for %s', async (_label, result, expectedNotice) => {
    mocked.deletePosition.mockResolvedValue(result);
    await mountQueue([position({ id: 'pos-1', symbol: 'AAPL' })]);
    await openDeleteDialog(cardFor('AAPL'));
    fireEvent.click(await screen.findByRole('button', { name: 'Delete round trip' }));
    const action: PendingAction = pendingActions.schedule.mock.calls[0][0];

    await runScheduledAction(action);

    expect(await screen.findByText(expectedNotice)).toBeInTheDocument();
  });

  it('reports the error message when the delete itself fails', async () => {
    mocked.deletePosition.mockRejectedValueOnce(new Error('delete failed server-side'));
    await mountQueue([position()]);
    await openDeleteDialog(cardFor('AAPL'));
    fireEvent.click(await screen.findByRole('button', { name: 'Delete round trip' }));
    const action: PendingAction = pendingActions.schedule.mock.calls[0][0];

    await runScheduledAction(action);

    expect(await screen.findByText('delete failed server-side')).toBeInTheDocument();
  });
});

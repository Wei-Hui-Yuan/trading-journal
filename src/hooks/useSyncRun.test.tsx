/**
 * The sync state machine: what happens between pressing the button and hearing
 * back, now that the two are no longer the same request.
 *
 * The server answers a browser with 202 and a run id, and the outcome arrives
 * later through a polled `sync_runs` row. That moved every side effect of a
 * finished sync -- cache invalidation, the toast, the badge -- out of the
 * mutation and into a watcher that infers "it finished" from a change in the
 * polled row. The correctness now rests on a transition rather than a callback,
 * and a transition has two ways to be wrong that a callback does not:
 *
 *   1. FIRING WHEN IT SHOULD NOT. On mount, `latest` is almost always a
 *      terminal run from hours ago. Acting on that invalidates every expensive
 *      query in the app and announces a sync nobody performed -- on every
 *      single page load.
 *   2. NEVER FIRING. If the poll stops while a row is still `running`, the badge
 *      spins forever and the ledger is never refreshed.
 *
 * ON HOW THIS IS DRIVEN. The status is written straight into the query cache and
 * the re-render is then forced with `rerender`, rather than waiting for React
 * Query to notify the mounted observer on its own. Under jsdom that notification
 * does not reliably land inside a single awaited `act`, and a test that
 * intermittently observes nothing is worse than no test -- especially here,
 * where "nothing happened" is what half of these cases assert. React Query's
 * notifier is its own concern and is exercised by the app itself; what is under
 * test is what the hook does when it re-renders with a changed row.
 */

import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  queryKeys,
  useSyncInFlight,
  useSyncRunWatcher,
  useSyncStatus,
} from '@/hooks/useTradeInbox';
import type { IngestResult, SyncRun, SyncStatus } from '@/types/api';

const holder = vi.hoisted(() => ({ current: null as unknown }));

// Resolves with whatever the test last wrote, so a refetch React Query decides
// to run on its own returns the same row rather than clobbering it.
vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  getSyncStatus: vi.fn(() => Promise.resolve(holder.current)),
}));

let client: QueryClient;

const wrapper = ({ children }: { children: React.ReactNode }) =>
  React.createElement(QueryClientProvider, { client }, children);

/** Mount a hook, and be able to re-render it on demand. */
function harness<T>(hook: () => T) {
  const seen: T[] = [];
  const Probe = () => {
    seen.push(hook());
    return null;
  };
  const { rerender } = render(React.createElement(Probe), { wrapper });
  return {
    latest: () => seen[seen.length - 1],
    /** Let the hook see the current cache contents. */
    advance: async () => {
      await act(async () => {
        rerender(React.createElement(Probe));
      });
    },
  };
}

const RESULT: IngestResult = {
  executions_parsed: 12,
  staged_new: 4,
  staged_duplicates: 8,
  trades_created: 4,
  trades_duplicates: 0,
  positions_matched: 2,
  symbols_touched: ['CRWD'],
  skipped_non_tradeable: 0,
  queries_failed: [],
};

const run = (over: Partial<SyncRun> = {}): SyncRun =>
  ({
    id: 'run-1',
    trigger: 'manual',
    outcome: 'success',
    started_at: '2026-08-19T12:00:00Z',
    finished_at: '2026-08-19T12:01:00Z',
    error: null,
    result: RESULT,
    ...over,
  }) as SyncRun;

const status = (latest: SyncRun | null, abandoned = false): SyncStatus =>
  ({ latest, latest_looks_abandoned: abandoned }) as SyncStatus;

const RUNNING = () => run({ outcome: 'running', finished_at: null });

/** State what the server currently says. */
function put(value: SyncStatus) {
  holder.current = value;
  client.setQueryData(queryKeys.syncStatus, value);
}

beforeEach(() => {
  client = new QueryClient({
    // staleTime keeps React Query from refetching underneath a test and
    // replacing the row it just stated.
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  holder.current = null;
});

afterEach(() => {
  client.clear();
});

describe('useSyncInFlight', () => {
  it('is false when nothing has ever run', () => {
    put(status(null));

    expect(harness(useSyncInFlight).latest()).toBe(false);
  });

  it('is true while a run is recorded as running', () => {
    put(status(RUNNING()));

    expect(harness(useSyncInFlight).latest()).toBe(true);
  });

  it('is false once the run reaches a terminal outcome', () => {
    put(status(run({ outcome: 'success' })));

    expect(harness(useSyncInFlight).latest()).toBe(false);
  });

  it('is false for a run that is running but abandoned', () => {
    // A recycled worker leaves the row `running` forever -- the server only
    // reaps on the next START, since a GET has no business writing rows. Left
    // as "in flight", the button stays disabled until someone syncs again.
    put(status(RUNNING(), true));

    expect(harness(useSyncInFlight).latest()).toBe(false);
  });

  it('follows the row as it changes', async () => {
    put(status(RUNNING()));
    const view = harness(useSyncInFlight);
    expect(view.latest()).toBe(true);

    put(status(run({ outcome: 'success' })));
    await view.advance();

    expect(view.latest()).toBe(false);
  });
});

describe('useSyncStatus polling', () => {
  /** The interval the hook would schedule for a given cache state. */
  function intervalFor(value: SyncStatus) {
    put(value);
    harness(useSyncStatus);
    const entry = client.getQueryCache().find({ queryKey: queryKeys.syncStatus })!;
    const { refetchInterval } = entry.options as {
      refetchInterval: (query: unknown) => number | false;
    };
    return refetchInterval({ state: { data: value } });
  }

  it('does not poll when nothing has run', () => {
    // A daily schedule does not justify a timer on every mounted page.
    expect(intervalFor(status(null))).toBe(false);
  });

  it('does not poll a run that already finished', () => {
    expect(intervalFor(status(run({ outcome: 'success' })))).toBe(false);
  });

  it('polls every three seconds while a run is in flight', () => {
    expect(intervalFor(status(RUNNING()))).toBe(3_000);
  });

  it('stops polling a run that looks abandoned', () => {
    // Otherwise this is an eternal 3s timer against a row nobody will finish.
    expect(intervalFor(status(RUNNING(), true))).toBe(false);
  });
});

describe('useSyncRunWatcher', () => {
  /** Records which query keys get invalidated, as dot-joined strings. */
  function watchInvalidations(): string[] {
    const invalidated: string[] = [];
    vi.spyOn(client, 'invalidateQueries').mockImplementation(((args: {
      queryKey: readonly unknown[];
    }) => {
      invalidated.push(args.queryKey.join('.'));
      return Promise.resolve();
    }) as never);
    return invalidated;
  }

  const lastSync = () =>
    client.getQueryData(queryKeys.lastSync) as
      | { outcome: string; summary: string; result: IngestResult | null; acknowledged: boolean }
      | undefined;

  it('does nothing for a terminal run that was already there on mount', async () => {
    // THE case the arming logic exists for. `latest` on a fresh page load is
    // yesterday's successful sync; reacting to it would invalidate the ledger,
    // the dashboard and every analytics window on every navigation, and announce
    // a sync that did not just happen.
    put(status(run({ outcome: 'success' })));
    const invalidated = watchInvalidations();

    const view = harness(useSyncRunWatcher);
    await view.advance();

    expect(invalidated).toEqual([]);
    expect(lastSync()).toBeUndefined();
  });

  it('does nothing for an error run that was already there on mount', async () => {
    put(status(run({ outcome: 'error', error: 'token expired' })));
    const invalidated = watchInvalidations();

    const view = harness(useSyncRunWatcher);
    await view.advance();

    expect(invalidated).toEqual([]);
    expect(lastSync()).toBeUndefined();
  });

  it('reports a run it watched go from running to success', async () => {
    put(status(RUNNING()));
    const invalidated = watchInvalidations();
    const view = harness(useSyncRunWatcher);

    put(status(run({ outcome: 'success' })));
    await view.advance();

    // The ledger moved -- CRWD was touched and four trades created.
    expect(invalidated).toContain('roundTrips');
    expect(invalidated).toContain('dashboardStats');
    expect(invalidated).toContain('advancedMetrics');
    expect(lastSync()).toMatchObject({
      outcome: 'success',
      summary: '4 new',
      acknowledged: false,
    });
  });

  it('catches a run started by the scheduler or another tab', async () => {
    // Same transition, different origin. The watcher cannot tell them apart and
    // should not: a 9pm run that failed is exactly as worth reporting.
    put(status(run({ trigger: 'cron', outcome: 'running', finished_at: null })));
    watchInvalidations();
    const view = harness(useSyncRunWatcher);

    put(status(run({ trigger: 'cron', outcome: 'error', error: 'expired token' })));
    await view.advance();

    expect(lastSync()).toMatchObject({ outcome: 'error', summary: 'expired token' });
  });

  it('says a run stopped reporting rather than inventing a failure', async () => {
    // The worker went away mid-run. The ingest never produced an error, so
    // claiming one would be a fabrication -- and leaving the badge spinning is
    // worse than either.
    put(status(RUNNING()));
    const view = harness(useSyncRunWatcher);

    put(status(RUNNING(), true));
    await view.advance();

    expect(lastSync()).toMatchObject({
      outcome: 'error',
      summary: 'sync stopped reporting',
    });
  });

  it('fires once per run, not on every poll after it finished', async () => {
    // Re-arming requires another `running`. Without that, every 3s poll after a
    // successful sync would invalidate the whole app again.
    put(status(RUNNING()));
    const invalidated = watchInvalidations();
    const view = harness(useSyncRunWatcher);

    put(status(run({ outcome: 'success' })));
    await view.advance();
    const afterFirst = invalidated.length;
    expect(afterFirst).toBeGreaterThan(0);

    // The same terminal row arriving again, as a repeated poll would deliver it.
    put(status(run({ outcome: 'success', finished_at: '2026-08-19T12:01:01Z' })));
    await view.advance();

    expect(invalidated.length).toBe(afterFirst);
  });

  it('reports a partial run as partial rather than green', async () => {
    // IBKR rate-limits report generation per token and its cooldown outlasts a
    // request, so a partial run reports fewer fills than exist. Green is how
    // "no new trades" comes to mean "IBKR refused us".
    put(status(RUNNING()));
    const view = harness(useSyncRunWatcher);

    put(
      status(
        run({
          outcome: 'partial',
          result: { ...RESULT, queries_failed: ['TCF timed out'] },
        })
      )
    );
    await view.advance();

    expect(lastSync()).toMatchObject({
      outcome: 'partial',
      summary: '1 query unavailable',
    });
  });

  it('does not invalidate when the sync changed nothing', async () => {
    // The ordinary run, and every scheduled one on a day without trading:
    // IBKR's rolling window re-reports fills already imported, so nothing is
    // promoted and nothing matched. Invalidating anyway refetches the app's most
    // expensive queries -- roundTrips is an infinite query and replays every
    // loaded page in sequence.
    put(status(RUNNING()));
    const invalidated = watchInvalidations();
    const view = harness(useSyncRunWatcher);

    put(
      status(
        run({
          result: {
            ...RESULT,
            staged_new: 0,
            trades_created: 0,
            trades_duplicates: 12,
            positions_matched: 0,
            symbols_touched: [],
            broker_figures_refreshed: false,
          } as IngestResult,
        })
      )
    );
    await view.advance();

    expect(invalidated).toEqual([]);
    // Still reported, though -- the badge has to say a sync happened.
    expect(lastSync()).toMatchObject({ outcome: 'success', summary: 'up to date' });
  });

  it('invalidates on refreshed broker figures alone', async () => {
    // The one signal implied by no counter. A refreshed cost basis moves open
    // exposure through _acquisition_premium in list_round_trips with every other
    // figure still zero, so gating on symbols_touched would miss it.
    put(status(RUNNING()));
    const invalidated = watchInvalidations();
    const view = harness(useSyncRunWatcher);

    put(
      status(
        run({
          result: {
            ...RESULT,
            trades_created: 0,
            symbols_touched: [],
            broker_figures_refreshed: true,
          } as IngestResult,
        })
      )
    );
    await view.advance();

    expect(invalidated).toContain('roundTrips');
  });

  it('assumes the ledger moved when the API is too old to say', async () => {
    // An absent broker_figures_refreshed means an API predating the field. "I do
    // not know" has to read as "assume it did", so a browser deployed ahead of
    // the API stays correct rather than quietly skipping refreshes.
    put(status(RUNNING()));
    const invalidated = watchInvalidations();
    const view = harness(useSyncRunWatcher);

    const withoutField: Partial<IngestResult> = {
      ...RESULT,
      trades_created: 0,
      symbols_touched: [],
    };
    delete (withoutField as Record<string, unknown>).broker_figures_refreshed;

    put(status(run({ result: withoutField as IngestResult })));
    await view.advance();

    expect(invalidated).toContain('roundTrips');
  });

  it('still refreshes when a finished run stored no result at all', async () => {
    // Without the payload there is no way to tell whether the ledger moved, and
    // guessing "it did" is the safe direction.
    put(status(RUNNING()));
    const invalidated = watchInvalidations();
    const view = harness(useSyncRunWatcher);

    put(status(run({ outcome: 'success', result: null })));
    await view.advance();

    expect(invalidated).toContain('roundTrips');
    expect(invalidated).toContain('dashboardStats');
    expect(invalidated).toContain('advancedMetrics');
  });

  it('renders a run recorded before a field existed without throwing', async () => {
    // sync_runs.result is a snapshot of whatever IngestResult looked like when
    // the row was written. The toast reads sixteen fields and indexes into three
    // arrays, so a partial payload has to be completed before it gets there --
    // and those historical rows are exactly what this feature exists to surface.
    put(status(RUNNING()));
    const view = harness(useSyncRunWatcher);

    put(status(run({ result: { trades_created: 2 } as unknown as IngestResult })));
    await view.advance();

    const stored = lastSync()!;
    expect(stored.summary).toBe('2 new');
    // Defaulted rather than undefined -- this is what stops the toast throwing.
    expect(stored.result!.symbols_touched).toEqual([]);
    expect(stored.result!.queries_failed).toEqual([]);
    expect(stored.result!.staged_duplicates).toBe(0);
    // And the stored value won over the default.
    expect(stored.result!.trades_created).toBe(2);
  });
});

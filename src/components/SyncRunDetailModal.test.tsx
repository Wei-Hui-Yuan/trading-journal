/**
 * The badge's sync detail modal, and the run-selection rule behind it.
 *
 * The case that motivated this component is the overnight scheduled run: it
 * finishes while the browser is closed, so `useSyncRunWatcher` never sees the
 * running -> finished transition and never writes it into `lastSync`. Its full
 * payload still arrives in `sync_runs.result`, and before this it had nowhere
 * to be rendered. Those tests seed only the SERVER cache entry, with no
 * in-memory sync at all, which is exactly that situation.
 */

import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SyncRunDetailModal } from '@/components/SyncRunDetailModal';
import type { LastSyncState, SyncStatus } from '@/types/api';

afterEach(cleanup);

const CRON_RUN: SyncStatus = {
  latest: {
    id: 'run-1',
    started_at: '2026-08-22T02:00:00Z',
    finished_at: '2026-08-22T02:01:00Z',
    trigger: 'cron',
    outcome: 'success',
    executions_parsed: 45,
    trades_created: 3,
    positions_matched: 2,
    plans_attached: 0,
    error: null,
    result: {
      executions_parsed: 45,
      trades_created: 3,
      staged_duplicates: 40,
      trades_duplicates: 2,
      positions_matched: 2,
      symbols_touched: ['AAPL'],
      skipped_non_tradeable: 0,
      queries_failed: [],
    },
  },
  last_success_at: '2026-08-22T02:01:00Z',
  seconds_since_success: 60,
  latest_looks_abandoned: false,
};

function mount(seed: { status?: SyncStatus; lastSync?: LastSyncState }, open = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (seed.status) client.setQueryData(['syncStatus'], seed.status);
  if (seed.lastSync) client.setQueryData(['lastSync'], seed.lastSync);
  const onClose = vi.fn();
  const view = render(
    <QueryClientProvider client={client}>
      <SyncRunDetailModal open={open} onClose={onClose} />
    </QueryClientProvider>
  );
  return { ...view, onClose };
}

const dialog = () => within(screen.getByRole('dialog'));

describe('SyncRunDetailModal', () => {
  it('renders nothing at all when closed', () => {
    mount({ status: CRON_RUN }, false);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('reports a scheduled run the browser never watched finish', async () => {
    // No `lastSync` seeded on purpose: this is the overnight-cron case.
    mount({ status: CRON_RUN });

    expect(await dialog().findByText('Sync complete')).toBeInTheDocument();
    expect(await dialog().findByText('45')).toBeInTheDocument();
    expect(await dialog().findByText('New fills imported')).toBeInTheDocument();
  });

  it('says the run was scheduled rather than leaving a bare timestamp', async () => {
    mount({ status: CRON_RUN });
    expect(await dialog().findByText(/scheduled/)).toBeInTheDocument();
  });

  it('sums both duplicate counters into one "already in the ledger" figure', async () => {
    // 40 stopped at staging + 2 at the trades insert are the same fact to the
    // reader; reporting only the second would read as 2.
    mount({ status: CRON_RUN });
    const row = (await dialog().findByText('Already in the ledger')).closest('div')!;
    expect(within(row).getByText('42')).toBeInTheDocument();
  });

  it('back-fills fields a historical row predates instead of throwing', async () => {
    // `completeResult` exists for rows written before a field did. Without it
    // the detail indexes into undefined arrays and the modal crashes on
    // exactly the old runs it exists to surface.
    const sparse = {
      ...CRON_RUN,
      latest: { ...CRON_RUN.latest!, result: { trades_created: 1 } },
    };
    mount({ status: sparse });
    expect(await dialog().findByText('New fills imported')).toBeInTheDocument();
    expect(await dialog().findByText('Executions IBKR returned')).toBeInTheDocument();
  });

  it('stamps the run in ET, matching the badge that opens it', async () => {
    // The badge reads "12:44 ET". Rendering the same run in the browser's own
    // zone here would put two different clock times on one sync. 02:00 UTC on
    // the 22nd is 22:00 EDT on the 21st, and the assertion is stable because
    // the formatter pins the zone rather than inheriting the runner's.
    mount({ status: CRON_RUN });
    expect(await dialog().findByText(/Aug 21, 22:00 ET/)).toBeInTheDocument();
  });

  it('closes on Escape', async () => {
    const { onClose } = mount({ status: CRON_RUN });
    await screen.findByRole('dialog');
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalled();
  });

  it('offers an honest empty state when nothing has finished', async () => {
    mount({ status: { latest: null, last_success_at: null, seconds_since_success: null, latest_looks_abandoned: false } });
    expect(await dialog().findByText('Nothing to report yet.')).toBeInTheDocument();
  });

  it('shows nothing to report while a run is still in flight', async () => {
    // A run in progress has no result yet, and claiming "never synced" during
    // one would be the single most misleading thing this modal could say.
    const running: SyncStatus = {
      ...CRON_RUN,
      latest: { ...CRON_RUN.latest!, outcome: 'running', finished_at: null, result: null },
    };
    mount({ status: running });
    expect(await dialog().findByText('Nothing to report yet.')).toBeInTheDocument();
  });

  it('prefers this tab\'s newer run over an older persisted one', async () => {
    const memory: LastSyncState = {
      at: '2026-08-22T09:00:00Z', // later than the 02:00 cron run
      outcome: 'error',
      status: 500,
      summary: 'The sync failed.',
      result: null,
      acknowledged: true, // dismissed toast: the modal must still show it
    };
    mount({ status: CRON_RUN, lastSync: memory });

    expect(await dialog().findByText('Sync failed')).toBeInTheDocument();
    expect(await dialog().findByText('The sync failed.')).toBeInTheDocument();
    expect(dialog().queryByText(/scheduled/)).not.toBeInTheDocument();
  });

  it('keeps the persisted run when it is newer than this tab\'s', async () => {
    const stale: LastSyncState = {
      at: '2026-08-21T09:00:00Z', // earlier than the 02:00 run on the 22nd
      outcome: 'error',
      status: 500,
      summary: 'The sync failed.',
      result: null,
      acknowledged: true,
    };
    mount({ status: CRON_RUN, lastSync: stale });
    expect(await dialog().findByText('Sync complete')).toBeInTheDocument();
  });
});

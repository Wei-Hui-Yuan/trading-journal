/**
 * Header: scoped to the pending-reviews badge only.
 *
 * That badge is the one thing this pass actually changed -- moved from its
 * own alert-styled slot into the status-badge group (left of IBKR Sync),
 * and stripped of the animate-pulse/animate-spin pair it used to carry.
 * Everything else here (SyncStatusBadge's six states, DataHealthBadge,
 * SyncBrokerButton's own states) is unchanged by this phase and stays
 * untested for now -- it gets real coverage in the phase that actually
 * touches it (the sync popover), rather than being backfilled here as a
 * side effect of a sizing pass.
 *
 * `getSyncStatus` is mocked to "never synced" so SyncStatusBadge renders
 * its simplest state and nothing here depends on sync internals.
 * `useLastAudit`/`useLastSync` never fetch -- they only read the query
 * cache -- so a fresh QueryClient already gives their empty states for
 * free, no mock needed.
 */

import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const NEVER_SYNCED = {
  latest: null,
  last_success_at: null,
  seconds_since_success: null,
  latest_looks_abandoned: false,
};

/**
 * Mutable so a test can put the badge into a state that has a run to open.
 *
 * Via `vi.hoisted` because `vi.mock` factories are hoisted above ordinary
 * module scope -- a plain `let` read from inside one is still in its temporal
 * dead zone when the factory runs.
 */
const state = vi.hoisted(() => ({ syncStatus: null as unknown }));

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  getSyncStatus: vi.fn(() => Promise.resolve(state.syncStatus)),
}));

vi.mock('@/components/SyncBrokerButton', () => ({
  SyncBrokerButton: () => React.createElement('div', null, 'sync-broker-stub'),
}));
vi.mock('@/components/PlanModal', () => ({
  PlanModal: () => null,
}));
vi.mock('@/components/SyncResultToast', () => ({
  SyncResultToast: () => null,
}));
vi.mock('@/components/DataHealthModal', () => ({
  DataHealthModal: () => null,
}));

import { Header } from '@/components/Header';

function mountHeader(pendingCount: number) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    React.createElement(
      QueryClientProvider,
      { client },
      React.createElement(Header, { pendingCount })
    )
  );
}

state.syncStatus = NEVER_SYNCED;

afterEach(() => {
  cleanup();
  state.syncStatus = NEVER_SYNCED;
});

describe('Header > pending reviews badge', () => {
  it('renders nothing when pendingCount is 0, same as before', () => {
    mountHeader(0);
    expect(screen.queryByText(/Pending Review/)).not.toBeInTheDocument();
  });

  it('shows the exact count and label when there is a backlog', async () => {
    mountHeader(3);
    expect(await screen.findByText('3 Pending Reviews')).toBeInTheDocument();
  });

  it('singular count still uses the plural label (unchanged wording, not part of this pass)', async () => {
    mountHeader(1);
    expect(await screen.findByText('1 Pending Reviews')).toBeInTheDocument();
  });

  it('carries neither animate-pulse nor animate-spin -- both were dropped deliberately', async () => {
    mountHeader(2);
    const badge = (await screen.findByText('2 Pending Reviews')).closest('div');
    expect(badge).not.toBeNull();
    expect(badge).not.toHaveClass('animate-pulse');
    // The badge's own icon must not spin either -- confirmed by walking every
    // descendant rather than trusting one selector, since a spinning icon
    // could in principle sit one level deeper than the badge's direct child.
    const spinning = badge!.querySelectorAll('.animate-spin');
    expect(spinning.length).toBe(0);
  });

  it('renders before (left of) the IBKR Sync badge in DOM order', async () => {
    mountHeader(4);
    const pending = await screen.findByText('4 Pending Reviews');
    const sync = await screen.findByText('IBKR Sync:');
    // compareDocumentPosition's DOCUMENT_POSITION_FOLLOWING bit is set when
    // the argument node comes AFTER the node compareDocumentPosition was
    // called on -- so this reads "sync comes after pending" exactly.
    expect(
      pending.compareDocumentPosition(sync) & Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
  });
});

describe('Header > IBKR sync badge', () => {
  const FINISHED = {
    latest: {
      id: 'r1',
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
        staged_duplicates: 0,
        trades_duplicates: 0,
        positions_matched: 2,
        symbols_touched: [],
        skipped_non_tradeable: 0,
        queries_failed: [],
      },
    },
    last_success_at: '2026-08-22T02:01:00Z',
    seconds_since_success: 60,
    latest_looks_abandoned: false,
  };

  it('stays an inert div when nothing has ever synced -- there is nothing to open', async () => {
    mountHeader(0);
    const label = await screen.findByText('IBKR Sync:');
    expect(label.closest('button')).toBeNull();
  });

  it('becomes a button once a run has finished', async () => {
    state.syncStatus = FINISHED;
    mountHeader(0);
    // Wait for the resolved run to render, not merely for the static label:
    // the never-synced branch also renders "IBKR Sync:" and is not a button.
    await screen.findByTitle(/Click for the full result/);
    expect(screen.getByText('IBKR Sync:').closest('button')).not.toBeNull();
  });

  it('opens the detail modal for that run when clicked', async () => {
    state.syncStatus = FINISHED;
    mountHeader(0);
    const badge = await screen.findByTitle(/Click for the full result/);

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    fireEvent.click(badge);

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Sync complete')).toBeInTheDocument();
    // The figure proves the modal read the SAME run the badge is describing,
    // rather than rendering some other source that happens to be present.
    expect(within(dialog).getByText('45')).toBeInTheDocument();
  });
});

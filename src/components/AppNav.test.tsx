/**
 * AppNav: the pending-reviews badge, and the route-driven parts of the bar.
 *
 * Was Header, and dashboard-only. Now rendered from the root layout on every
 * page, which is what the active tab and the page title block depend on --
 * both read `usePathname`, so both are mocked per test rather than routed.
 *
 * SyncStatusBadge's six states and SyncBrokerButton's own states remain
 * untested here; they are covered where they are actually exercised
 * (SyncRunDetailModal.test.tsx) rather than backfilled through the nav.
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
const state = vi.hoisted(() => ({ syncStatus: null as unknown, pathname: '/' }));

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

vi.mock('next/navigation', () => ({
  usePathname: () => state.pathname,
}));

import { AppNav } from '@/components/AppNav';

/**
 * `pendingCount` is no longer a prop -- AppNav owns the query, because a nav
 * on every page cannot rely on one route to fetch for it. Tests set the count
 * by seeding what that query returns.
 */
function mountNav(pendingCount = 0, pathname = '/') {
  state.pathname = pathname;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(
    ['positions', 'pending'],
    Array.from({ length: pendingCount }, (_, i) => ({ id: `p${i}` }))
  );
  return render(
    React.createElement(QueryClientProvider, { client }, React.createElement(AppNav))
  );
}

/** Older name, kept so the badge tests below read unchanged. */
const mountHeader = (pendingCount: number) => mountNav(pendingCount);

state.syncStatus = NEVER_SYNCED;

afterEach(() => {
  cleanup();
  state.syncStatus = NEVER_SYNCED;
  state.pathname = '/';
});

describe('AppNav > pending reviews badge', () => {
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

describe('AppNav > IBKR sync badge', () => {
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

describe('AppNav > page identity', () => {
  it('titles the dashboard with the product name, and brands it', async () => {
    mountNav(0, '/');
    expect(await screen.findByRole('heading', { level: 1 })).toHaveTextContent(
      'TRADING JOURNAL'
    );
    expect(screen.getByText('PRO')).toBeInTheDocument();
  });

  it('titles each other route from the same table that builds the tabs', async () => {
    mountNav(0, '/analytics');
    const h1 = await screen.findByRole('heading', { level: 1 });
    expect(h1).toHaveTextContent('ANALYTICS & REVIEW');
    expect(
      screen.getByText('R-multiples, slippage, and behavioural attribution')
    ).toBeInTheDocument();
  });

  it('drops the PRO badge off non-brand pages', () => {
    mountNav(0, '/journal');
    // "ANALYTICS & REVIEW  PRO" would read as a tier of the page, not the app.
    expect(screen.queryByText('PRO')).not.toBeInTheDocument();
  });

  it('titles settings too, though it has no tab of its own', async () => {
    mountNav(0, '/settings');
    expect(await screen.findByRole('heading', { level: 1 })).toHaveTextContent('SETTINGS');
    expect(screen.queryByRole('link', { name: 'Settings' })).toBeInTheDocument();
  });

  it('renders exactly one h1, since it is now the only one on any page', () => {
    mountNav(0, '/sizing');
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
  });

  it('falls back to the product name on an unknown route', async () => {
    // /_not-found has no entry, and a blank title bar would be worse than the
    // app's own name.
    mountNav(0, '/nope');
    expect(await screen.findByRole('heading', { level: 1 })).toHaveTextContent(
      'TRADING JOURNAL'
    );
  });

  it('points the logo home, which is what replaced six back-links', () => {
    mountNav(0, '/journal');
    expect(screen.getByRole('link', { name: 'Dashboard' })).toHaveAttribute('href', '/');
  });
});

describe('AppNav > active tab', () => {
  it('marks the current route, and only it', () => {
    mountNav(0, '/analytics');
    const active = screen.getByRole('link', { name: 'Analytics' });
    expect(active).toHaveAttribute('aria-current', 'page');
    expect(active.className).toContain('border-win');

    for (const other of ['Journal', 'Strategies', 'Sizing', 'Portfolio']) {
      expect(screen.getByRole('link', { name: other })).not.toHaveAttribute('aria-current');
    }
  });

  it('marks nothing when the route has no tab', () => {
    // Settings is reached by the gear icon; no tab should light up for it, and
    // the dashboard tab must not either -- '/' is not a prefix match.
    mountNav(0, '/settings');
    for (const label of ['Journal', 'Analytics', 'Strategies', 'Sizing', 'Portfolio']) {
      expect(screen.getByRole('link', { name: label })).not.toHaveAttribute('aria-current');
    }
  });

  it('marks nothing on the dashboard, which has no tab of its own', () => {
    mountNav(0, '/');
    for (const label of ['Journal', 'Analytics', 'Strategies', 'Sizing', 'Portfolio']) {
      expect(screen.getByRole('link', { name: label })).not.toHaveAttribute('aria-current');
    }
  });
});

describe('AppNav > route matching', () => {
  it('keeps a section tab lit on a child route', () => {
    // No nested routes exist yet, which is exactly why this is pinned: exact
    // equality passes every test today and silently un-highlights the tab the
    // day '/journal/[id]' lands.
    mountNav(0, '/journal/abc-123');
    expect(screen.getByRole('link', { name: 'Journal' })).toHaveAttribute(
      'aria-current',
      'page'
    );
  });

  it('titles a child route from its section, not the fallback', async () => {
    mountNav(0, '/journal/abc-123');
    expect(await screen.findByRole('heading', { level: 1 })).toHaveTextContent(
      'TRADE JOURNAL'
    );
  });

  it('does not treat a same-prefix sibling as a child', () => {
    // '/sizing-experiment' is not under '/sizing'. A bare startsWith says it is.
    mountNav(0, '/sizing-experiment');
    expect(screen.getByRole('link', { name: 'Sizing' })).not.toHaveAttribute(
      'aria-current'
    );
  });

  it('never lets the dashboard swallow another route', async () => {
    // Every path startsWith '/', so the dashboard entry has to match exactly
    // or it would claim the title block on every page in the app.
    mountNav(0, '/strategies');
    expect(await screen.findByRole('heading', { level: 1 })).toHaveTextContent(
      'STRATEGY PLAYBOOK'
    );
  });
});

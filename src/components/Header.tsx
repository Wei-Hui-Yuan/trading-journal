'use client';

import React, { useState } from 'react';
import Link from 'next/link';
import { Activity, BarChart3, BookOpen, BookText, Calculator, ClipboardList, Landmark, RefreshCw, SlidersHorizontal, User } from 'lucide-react';
import { SyncBrokerButton } from './SyncBrokerButton';
import { PlanModal } from './PlanModal';
import { SyncResultToast } from './SyncResultToast';
import { DataHealthModal } from './DataHealthModal';
import {
  useLastSync,
  useSyncRunWatcher,
  useSyncStatus,
} from '@/hooks/useTradeInbox';
import { useLastAudit } from '@/hooks/useDataAudit';

interface HeaderProps {
  pendingCount: number;
}

/** Wall-clock time of the sync, in the market timezone the app reports in. */
const syncTimeFormatter = new Intl.DateTimeFormat('en-US', {
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
  timeZone: 'America/New_York',
});

/**
 * How long the ledger may go without a SUCCESSFUL sync before the badge says
 * so.
 *
 * Sized around the weekend, not around the schedule. A weekday-only job that
 * last succeeded Friday morning is not due again until Monday morning -- 72
 * hours later -- so anything tighter cries wolf every weekend, and an alert
 * that is routinely wrong is one that stops being read. 80 hours clears that
 * gap with slack for a late run.
 *
 * The cost is latency: a schedule that breaks on Monday is not flagged until
 * Thursday. Worth tightening once the cron has a track record, but a false
 * alarm every Saturday would train the warning away entirely.
 */
const SYNC_STALE_AFTER_SECONDS = 80 * 60 * 60;

/**
 * What the last broker sync actually did.
 *
 * Replaces a hardcoded "CONNECTED" that was never derived from anything — it
 * showed green while the sync was silently duplicating fills, and would have
 * shown green with the API down. Before any sync runs it says so rather than
 * asserting a health it has not verified.
 *
 * Reads the SERVER's record first (`useSyncStatus`), falling back to this
 * tab's in-memory one. That order is the point: `useLastSync` only ever knew
 * about syncs this browser session performed, so a scheduled run was invisible
 * and an unattended failure — an expired token, say — looked exactly like a
 * quiet market. The in-memory value still wins when it is NEWER, because a
 * sync that just finished should show instantly rather than after a refetch.
 */
const SyncStatusBadge: React.FC = () => {
  const lastSync = useLastSync();
  const { data: status } = useSyncStatus();

  const persisted = status?.latest ?? null;

  // Prefer whichever actually happened last. Normally that is the in-memory
  // one during a session where you pressed the button, and the persisted one
  // on a fresh page load or after the schedule ran.
  const useMemory =
    lastSync !== undefined &&
    (persisted === null ||
      new Date(lastSync.at).getTime() >= new Date(persisted.started_at).getTime());

  if (!lastSync && !persisted) {
    return (
      <div className="flex items-center space-x-2 px-4 py-3 rounded-lg bg-obsidian-bg border border-obsidian-border text-sm font-mono">
        <span className="h-2 w-2 rounded-full bg-slate-600" />
        <span className="text-slate-300">IBKR Sync:</span>
        <span className="text-obsidian-muted">never</span>
      </div>
    );
  }

  // A run in flight outranks both sources. It is the newest thing that has
  // happened by definition, and while it is happening neither the in-memory
  // record nor the previous row describes the current state of the ledger.
  //
  // `latest_looks_abandoned` is excluded deliberately: a run whose worker went
  // away is not still running, and rendering it as "syncing" would leave a
  // spinner up until somebody pressed the button again.
  const running =
    persisted?.outcome === 'running' && !status?.latest_looks_abandoned;

  if (running) {
    return (
      <div className="flex items-center space-x-2 px-4 py-3 rounded-lg bg-obsidian-bg border border-slate-600 text-sm font-mono">
        <span className="h-2 w-2 rounded-full bg-slate-300 animate-pulse" />
        <span className="text-slate-300">IBKR Sync:</span>
        <span className="text-slate-300">
          syncing
          {persisted!.trigger === 'cron' ? ' · scheduled' : ''}
        </span>
      </div>
    );
  }

  const outcome = useMemory ? lastSync!.outcome : persisted!.outcome;
  const at = useMemory ? lastSync!.at : persisted!.started_at;
  const summary = useMemory
    ? lastSync!.summary
    : persisted!.error ??
      (persisted!.trades_created > 0
        ? `${persisted!.trades_created} new`
        : `${persisted!.executions_parsed} returned`);

  // Stale beats fresh-but-red: a run that failed five minutes ago and a ledger
  // that has been un-synced for four days are different problems, and the
  // second is the one a schedule is supposed to prevent. Only a genuine
  // success clears it — a partial run leaves fills at the broker that never
  // reached the ledger.
  const stale =
    status !== undefined &&
    (status.seconds_since_success === null ||
      status.seconds_since_success > SYNC_STALE_AFTER_SECONDS);

  const tone = stale
    ? { dot: 'bg-amber-400', text: 'text-amber-300', border: 'border-amber-500/60' }
    : outcome === 'success'
      ? { dot: 'bg-win', text: 'text-win', border: 'border-obsidian-border' }
      : outcome === 'partial'
        ? { dot: 'bg-amber-400', text: 'text-amber-300', border: 'border-amber-500/40' }
        : { dot: 'bg-loss', text: 'text-loss', border: 'border-loss/40' };

  const clock = syncTimeFormatter.format(new Date(at));
  // The status code only means something when the server answered, and only
  // this tab ever has one — a run read back from the record has no HTTP
  // response attached to it.
  const code = useMemory
    ? lastSync!.status !== null
      ? ` · ${lastSync!.status}`
      : ' · no response'
    : '';
  // Saying which is not cosmetic: "the schedule ran and found nothing" and
  // "nothing has run since you last pressed the button" are the two states
  // this badge exists to separate.
  const source = !useMemory && persisted!.trigger === 'cron' ? ' · scheduled' : '';

  const days =
    status?.seconds_since_success != null
      ? Math.floor(status.seconds_since_success / 86_400)
      : null;

  return (
    <div
      className={`flex items-center space-x-2 px-4 py-3 rounded-lg bg-obsidian-bg border ${tone.border} text-sm font-mono`}
      title={
        (stale
          ? days === null
            ? 'No sync has ever completed successfully. '
            : `No successful sync for ${days} day${days === 1 ? '' : 's'}. `
          : '') + `${summary} — ${new Date(at).toLocaleString()}`
      }
    >
      <span className={`h-2 w-2 rounded-full ${tone.dot}`} />
      <span className="text-slate-300">IBKR Sync:</span>
      <span className={`${tone.text} font-semibold`}>
        {clock} ET{code}
        {source}
      </span>
      <span className="text-obsidian-muted">
        {stale ? (days === null ? 'never succeeded' : `stale · ${days}d`) : summary}
      </span>
    </div>
  );
};

/**
 * Whether the ledger's derived state still agrees with its fills.
 *
 * Tri-state, not a percentage: the checks behind it count unlike things --
 * stale round trips, unverified legs, unimportable fills -- and averaging
 * them into "97% healthy" would invent a quantity that does not exist.
 *
 * Says "not run" until it has actually run, exactly like SyncStatusBadge
 * above. A green badge on page load would be asserting a health nothing had
 * checked, which is the specific failure the hardcoded "CONNECTED" badge used
 * to have.
 */
const DataHealthBadge: React.FC<{ onOpen: () => void }> = ({ onOpen }) => {
  const audit = useLastAudit();

  const tone = !audit
    ? { dot: 'bg-slate-600', text: 'text-obsidian-muted', border: 'border-obsidian-border' }
    : audit.status === 'clean'
      ? { dot: 'bg-win', text: 'text-win', border: 'border-obsidian-border' }
      : audit.status === 'attention'
        ? { dot: 'bg-amber-400', text: 'text-amber-300', border: 'border-amber-500/40' }
        : { dot: 'bg-loss', text: 'text-loss', border: 'border-loss/40' };

  const flagged = audit
    ? audit.checks.filter((c) => c.status !== 'clean').length
    : 0;

  return (
    <button
      type="button"
      onClick={onOpen}
      title="FIFO and broker reconciliation — does stored state still agree with the fills?"
      className={`flex items-center space-x-2 px-4 py-3 rounded-lg bg-obsidian-bg border ${tone.border} text-sm font-mono transition-colors hover:border-slate-600`}
    >
      <span className={`h-2 w-2 rounded-full ${tone.dot}`} />
      <span className="text-slate-300">Data:</span>
      <span className={`${tone.text} font-semibold`}>
        {!audit
          ? 'not audited'
          : audit.status === 'clean'
            ? 'ties out'
            : `${flagged} check${flagged === 1 ? '' : 's'} flagged`}
      </span>
    </button>
  );
};

export const Header: React.FC<HeaderProps> = ({ pendingCount }) => {
  const [isPlanOpen, setIsPlanOpen] = useState(false);
  const [isHealthOpen, setIsHealthOpen] = useState(false);

  // MOUNTED HERE, AND NOWHERE ELSE. A sync now finishes in the background, so
  // something has to notice and do what the mutation's onSuccess used to:
  // refresh what the run changed, and raise the toast. This hook performs those
  // side effects, so a second copy would double them -- and the Header is the
  // one component guaranteed to be mounted on every page, which is what makes a
  // run started here still get reported after navigating away.
  useSyncRunWatcher();

  // Nav links, in one place so the two rows below cannot list them in a
  // different order from each other. `plain` marks a Link needing no
  // per-item box — the tab row below draws its own hover/underline instead.
  const NAV_LINKS = [
    { href: '/journal', label: 'Journal', icon: BookText },
    { href: '/analytics', label: 'Analytics', icon: BarChart3 },
    { href: '/strategies', label: 'Strategies', icon: BookOpen },
    { href: '/sizing', label: 'Sizing', icon: Calculator },
    { href: '/investments', label: 'Portfolio', icon: Landmark },
  ] as const;

  return (
    <header className="border-b border-obsidian-border bg-obsidian-card/80 backdrop-blur-md sticky top-0 z-50">
      {/* Row 1: identity, live status, account. Nothing here is navigation --
          it is either who this is (branding, avatar) or what state the app
          is in right now (sync, data health, pending reviews). */}
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">

        <div className="flex items-center space-x-3">
          <div className="h-9 w-9 rounded-xl bg-gradient-to-br from-win/20 to-emerald-900/40 border border-win/30 flex items-center justify-center shadow-win-glow">
            <Activity className="h-5 w-5 text-win" />
          </div>
          <div>
            <div className="flex items-center space-x-2">
              <span className="font-bold text-lg tracking-wider text-white">TRADING JOURNAL</span>
              <span className="text-[10px] uppercase font-mono px-2 py-0.5 rounded bg-win/10 text-win border border-win/20">PRO</span>
            </div>
            <p className="text-xs text-obsidian-muted font-medium">IBKR Gateway Execution Engine</p>
          </div>
        </div>

        {/* The "Regime: Bull Trending" badge that used to sit here was static
            text — it never consulted anything and read as live market state. */}
        <div className="flex items-center space-x-3">
          {/* Grouped with the two status badges, not styled as an alert
              anymore: the amber-950/pulse/spin treatment it used to have here
              duplicated the KPI strip's own "Inbox Queue" card (now removed)
              and borrowed the same busy-spinner vocabulary SyncBrokerButton
              uses for a sync actually in progress -- a static count sitting
              beside "syncing" made the two impossible to tell apart at a
              glance. Left of IBKR Sync because it is the more actionable of
              the three: reviews waiting on you outrank status you are only
              checking. Hidden entirely at zero, same as before. */}
          <div className="hidden md:flex items-center space-x-3">
            {pendingCount > 0 && (
              <div className="flex items-center space-x-2 px-4 py-3 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-400 text-sm font-mono">
                <RefreshCw className="h-4 w-4" />
                <span>{pendingCount} Pending Reviews</span>
              </div>
            )}
            <SyncStatusBadge />
            <DataHealthBadge onOpen={() => setIsHealthOpen(true)} />
          </div>

          <Link
            href="/settings"
            aria-label="Settings"
            className="p-3 rounded-lg bg-obsidian-bg border border-obsidian-border text-obsidian-muted hover:text-slate-200 hover:border-slate-700 transition"
          >
            <SlidersHorizontal className="h-5 w-5" />
          </Link>

          <div className="h-11 w-11 rounded-lg bg-slate-800 border border-obsidian-border flex items-center justify-center text-slate-300 font-semibold text-xs">
            <User className="h-5 w-5 text-slate-400" />
          </div>
        </div>
      </div>

      {/* Row 2: everything that is either "go somewhere" (the tab strip) or
          "do something right now" (Plan Trade, Sync Broker) -- kept apart
          from row 1's status indicators, which are neither. */}
      <div className="border-t border-obsidian-border">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-14 flex items-center justify-between">
          <nav className="flex items-center space-x-5 overflow-x-auto">
            {NAV_LINKS.map(({ href, label, icon: Icon }) => (
              <Link
                key={href}
                href={href}
                className="inline-flex items-center gap-1.5 py-3 text-sm font-medium text-obsidian-muted whitespace-nowrap border-b-2 border-transparent hover:text-slate-100 hover:border-slate-600 transition-colors"
              >
                <Icon className="h-4 w-4" />
                {label}
              </Link>
            ))}
          </nav>

          <div className="flex items-center space-x-2 pl-3">
            {/* Was "Manual Log", which wrote straight into `trades` and so
                could duplicate a fill the sync was about to import. It
                records a plan now — the same form, minus the execution
                fields it had no way to know yet. */}
            <button
              type="button"
              onClick={() => setIsPlanOpen(true)}
              className="inline-flex items-center gap-1.5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm font-medium text-amber-300 transition-colors hover:bg-amber-500/20"
            >
              <ClipboardList className="h-4 w-4" />
              <span className="hidden sm:inline">Plan Trade</span>
            </button>

            <SyncBrokerButton />
          </div>
        </div>
      </div>

      <PlanModal open={isPlanOpen} onClose={() => setIsPlanOpen(false)} />

      <DataHealthModal open={isHealthOpen} onClose={() => setIsHealthOpen(false)} />

      {/* Mounted here so the summary survives navigating between pages while a
          sync is still in flight — the request outlives any one route. */}
      <SyncResultToast />
    </header>
  );
};

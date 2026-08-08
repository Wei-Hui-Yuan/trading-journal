'use client';

import React, { useState } from 'react';
import Link from 'next/link';
import { Activity, BarChart3, BookOpen, BookText, Calculator, ClipboardList, Landmark, RefreshCw, SlidersHorizontal, User } from 'lucide-react';
import { SyncBrokerButton } from './SyncBrokerButton';
import { PlanModal } from './PlanModal';
import { SyncResultToast } from './SyncResultToast';
import { DataHealthModal } from './DataHealthModal';
import { useLastSync } from '@/hooks/useTradeInbox';
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
 * What the last broker sync actually did.
 *
 * Replaces a hardcoded "CONNECTED" that was never derived from anything — it
 * showed green while the sync was silently duplicating fills, and would have
 * shown green with the API down. Before any sync runs this session it says so
 * rather than asserting a health it has not verified.
 */
const SyncStatusBadge: React.FC = () => {
  const lastSync = useLastSync();

  if (!lastSync) {
    return (
      <div className="flex items-center space-x-2 px-3 py-1.5 rounded-lg bg-obsidian-bg border border-obsidian-border text-xs font-mono">
        <span className="h-2 w-2 rounded-full bg-slate-600" />
        <span className="text-slate-300">IBKR Sync:</span>
        <span className="text-obsidian-muted">not run yet</span>
      </div>
    );
  }

  const tone =
    lastSync.outcome === 'success'
      ? { dot: 'bg-win', text: 'text-win', border: 'border-obsidian-border' }
      : lastSync.outcome === 'partial'
        ? { dot: 'bg-amber-400', text: 'text-amber-300', border: 'border-amber-500/40' }
        : { dot: 'bg-loss', text: 'text-loss', border: 'border-loss/40' };

  const at = syncTimeFormatter.format(new Date(lastSync.at));
  // The status code only means something when the server answered. A failure
  // with no response is a different problem and must not read as "HTTP null".
  const code = lastSync.status !== null ? ` · ${lastSync.status}` : ' · no response';

  return (
    <div
      className={`flex items-center space-x-2 px-3 py-1.5 rounded-lg bg-obsidian-bg border ${tone.border} text-xs font-mono`}
      title={`${lastSync.summary}${
        lastSync.status !== null ? ` (HTTP ${lastSync.status})` : ' (no response)'
      } — ${new Date(lastSync.at).toLocaleString()}`}
    >
      <span className={`h-2 w-2 rounded-full ${tone.dot}`} />
      <span className="text-slate-300">IBKR Sync:</span>
      <span className={`${tone.text} font-semibold`}>
        {at} ET{code}
      </span>
      <span className="text-obsidian-muted">{lastSync.summary}</span>
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
      className={`flex items-center space-x-2 px-3 py-1.5 rounded-lg bg-obsidian-bg border ${tone.border} text-xs font-mono transition-colors hover:border-slate-600`}
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
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-14 flex items-center justify-between">

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
          <div className="hidden md:flex items-center space-x-3">
            <SyncStatusBadge />
            <DataHealthBadge onOpen={() => setIsHealthOpen(true)} />
          </div>

          {pendingCount > 0 && (
            <div className="flex items-center space-x-2 px-3 py-1.5 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-400 text-xs font-mono animate-pulse">
              <RefreshCw className="h-3.5 w-3.5 animate-spin" />
              <span>{pendingCount} Pending Reviews</span>
            </div>
          )}

          <Link
            href="/settings"
            aria-label="Settings"
            className="p-2 rounded-lg bg-obsidian-bg border border-obsidian-border text-obsidian-muted hover:text-slate-200 hover:border-slate-700 transition"
          >
            <SlidersHorizontal className="h-4 w-4" />
          </Link>

          <div className="h-8 w-8 rounded-lg bg-slate-800 border border-obsidian-border flex items-center justify-center text-slate-300 font-semibold text-xs">
            <User className="h-4 w-4 text-slate-400" />
          </div>
        </div>
      </div>

      {/* Row 2: everything that is either "go somewhere" (the tab strip) or
          "do something right now" (Plan Trade, Sync Broker) -- kept apart
          from row 1's status indicators, which are neither. */}
      <div className="border-t border-obsidian-border">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-11 flex items-center justify-between">
          <nav className="flex items-center space-x-5 overflow-x-auto">
            {NAV_LINKS.map(({ href, label, icon: Icon }) => (
              <Link
                key={href}
                href={href}
                className="inline-flex items-center gap-1.5 py-1 text-xs font-medium text-obsidian-muted whitespace-nowrap border-b-2 border-transparent hover:text-slate-100 hover:border-slate-600 transition-colors"
              >
                <Icon className="h-3.5 w-3.5" />
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
              className="inline-flex items-center gap-1.5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-xs font-medium text-amber-300 transition-colors hover:bg-amber-500/20"
            >
              <ClipboardList className="h-3.5 w-3.5" />
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

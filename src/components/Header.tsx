'use client';

import React, { useState } from 'react';
import Link from 'next/link';
import { Activity, BarChart3, BookOpen, BookText, ClipboardList, Landmark, RefreshCw, SlidersHorizontal, User } from 'lucide-react';
import { SyncBrokerButton } from './SyncBrokerButton';
import { CreatePlanModal } from './CreatePlanModal';
import { SyncResultToast } from './SyncResultToast';
import { useLastSync } from '@/hooks/useTradeInbox';

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

export const Header: React.FC<HeaderProps> = ({ pendingCount }) => {
  const [isPlanOpen, setIsPlanOpen] = useState(false);

  return (
    <header className="border-b border-obsidian-border bg-obsidian-card/80 backdrop-blur-md sticky top-0 z-50">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
        
        {/* Left Branding */}
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

        {/* Center Indicators.
            The "Regime: Bull Trending" badge that used to sit here was static
            text — it never consulted anything and read as live market state. */}
        <div className="hidden md:flex items-center space-x-6">
          <SyncStatusBadge />

          {pendingCount > 0 && (
            <div className="flex items-center space-x-2 px-3 py-1.5 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-400 text-xs font-mono animate-pulse">
              <RefreshCw className="h-3.5 w-3.5 animate-spin" />
              <span>{pendingCount} Pending Reviews</span>
            </div>
          )}
        </div>

        {/* Right Controls */}
        <div className="flex items-center space-x-3">
          <Link
            href="/journal"
            className="inline-flex items-center gap-2 rounded-lg bg-obsidian-bg border border-obsidian-border px-3 py-2 text-xs font-medium text-obsidian-muted hover:text-slate-100 hover:border-slate-600 transition-colors"
          >
            <BookText className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">Journal</span>
          </Link>

          {/* The long-term book. A separate destination rather than a filter on
              the journal: it answers "what is this worth" where the journal
              answers "did I follow the plan", and the two share no schema. */}
          <Link
            href="/investments"
            className="inline-flex items-center gap-2 rounded-lg bg-obsidian-bg border border-obsidian-border px-3 py-2 text-xs font-medium text-obsidian-muted hover:text-slate-100 hover:border-slate-600 transition-colors"
          >
            <Landmark className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">Portfolio</span>
          </Link>

          <Link
            href="/analytics"
            className="inline-flex items-center gap-2 rounded-lg bg-obsidian-bg border border-obsidian-border px-3 py-2 text-xs font-medium text-obsidian-muted hover:text-slate-100 hover:border-slate-600 transition-colors"
          >
            <BarChart3 className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">Analytics</span>
          </Link>

          <Link
            href="/strategies"
            className="inline-flex items-center gap-2 rounded-lg bg-obsidian-bg border border-obsidian-border px-3 py-2 text-xs font-medium text-obsidian-muted hover:text-slate-100 hover:border-slate-600 transition-colors"
          >
            <BookOpen className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">Strategies</span>
          </Link>

          {/* Was "Manual Log", which wrote straight into `trades` and so could
              duplicate a fill the sync was about to import. It records a plan
              now — the same form, minus the execution fields it had no way to
              know yet. */}
          <button
            type="button"
            onClick={() => setIsPlanOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs font-medium text-amber-300 transition-colors hover:bg-amber-500/20"
          >
            <ClipboardList className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">Plan Trade</span>
          </button>

          <SyncBrokerButton />

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

      <CreatePlanModal open={isPlanOpen} onClose={() => setIsPlanOpen(false)} />

      {/* Mounted here so the summary survives navigating between pages while a
          sync is still in flight — the request outlives any one route. */}
      <SyncResultToast />
    </header>
  );
};

'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshCw, Check, X, DownloadCloud } from 'lucide-react';
import { useSyncBroker } from '@/hooks/useTradeInbox';

type SyncState = 'idle' | 'syncing' | 'success' | 'error';

const RESET_DELAY_MS = 3000;

export const SyncBrokerButton: React.FC = () => {
  const [state, setState] = useState<SyncState>('idle');
  const [detail, setDetail] = useState<string | null>(null);

  // Track the reset timer and mount status so a state flash never lands after
  // the component unmounts.
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const isMounted = useRef(true);

  const syncMutation = useSyncBroker();

  useEffect(() => {
    isMounted.current = true;
    return () => {
      isMounted.current = false;
      if (resetTimer.current) clearTimeout(resetTimer.current);
    };
  }, []);

  const scheduleReset = useCallback(() => {
    if (resetTimer.current) clearTimeout(resetTimer.current);
    resetTimer.current = setTimeout(() => {
      if (!isMounted.current) return;
      setState('idle');
      setDetail(null);
    }, RESET_DELAY_MS);
  }, []);

  const handleSync = useCallback(async () => {
    // Guard against double-submits even if the disabled attribute is bypassed.
    if (state === 'syncing') return;

    setState('syncing');
    setDetail(null);

    try {
      // The hook invalidates the positions queue and dashboard on success, so
      // the cache is already refreshing by the time this resolves.
      const result = await syncMutation.mutateAsync();

      if (!isMounted.current) return;
      setState('success');
      // Distinguish "found new fills" from "already up to date": a run where
      // everything was rejected as a duplicate is a healthy no-op, not a miss.
      setDetail(
        result.trades_created > 0
          ? `${result.trades_created} new`
          : result.staged_duplicates > 0
            ? 'up to date'
            : 'no fills'
      );
      scheduleReset();
    } catch (err) {
      console.error('Broker ingest failed:', err);
      if (!isMounted.current) return;
      setState('error');
      setDetail(err instanceof Error ? err.message : null);
      scheduleReset();
    }
  }, [state, scheduleReset, syncMutation]);

  const isSyncing = state === 'syncing';

  const label =
    state === 'syncing'
      ? 'Syncing Broker...'
      : state === 'success'
        ? 'Synced ✓'
        : state === 'error'
          ? 'Failed ✗'
          : 'Sync Broker';

  // Glassmorphic base: translucent fill + blur + hairline top highlight.
  const base =
    'group relative inline-flex items-center gap-2 rounded-lg px-3.5 py-2 text-xs font-medium ' +
    'backdrop-blur-md border transition-all duration-300 ease-out select-none ' +
    'focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-obsidian-card';

  const stateStyles: Record<SyncState, string> = {
    idle:
      'bg-obsidian-bg/60 border-obsidian-border text-obsidian-muted shadow-sm ' +
      'hover:text-slate-100 hover:border-slate-600 hover:bg-white/[0.06] hover:shadow-md ' +
      'active:scale-[0.98] focus-visible:ring-slate-500',
    syncing:
      'bg-obsidian-bg/70 border-slate-700 text-slate-300 cursor-not-allowed ' +
      'opacity-80 animate-pulse focus-visible:ring-slate-500',
    success:
      'bg-win-glow border-win-border text-win shadow-win-glow focus-visible:ring-win',
    error:
      'bg-loss-glow border-loss-border text-loss shadow-loss-glow focus-visible:ring-loss',
  };

  const icon =
    state === 'syncing' ? (
      <RefreshCw className="h-3.5 w-3.5 animate-spin" />
    ) : state === 'success' ? (
      <Check className="h-3.5 w-3.5" />
    ) : state === 'error' ? (
      <X className="h-3.5 w-3.5" />
    ) : (
      <DownloadCloud className="h-3.5 w-3.5 transition-transform duration-300 group-hover:-translate-y-px" />
    );

  return (
    <button
      type="button"
      onClick={handleSync}
      disabled={isSyncing}
      aria-busy={isSyncing}
      aria-live="polite"
      title={detail ?? 'Pull the latest executions from IBKR'}
      className={`${base} ${stateStyles[state]}`}
    >
      {/* Top hairline highlight for the glass effect */}
      <span
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 top-0 h-px rounded-t-lg bg-gradient-to-r from-transparent via-white/10 to-transparent"
      />
      {icon}
      <span className="whitespace-nowrap">{label}</span>
      {state === 'success' && detail && (
        <span className="font-mono text-[10px] opacity-70">{detail}</span>
      )}
    </button>
  );
};

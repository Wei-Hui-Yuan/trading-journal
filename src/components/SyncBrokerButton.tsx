'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshCw, Check, X, DownloadCloud, AlertTriangle } from 'lucide-react';
import { useSyncBroker, useSyncInFlight, useSyncStatus } from '@/hooks/useTradeInbox';

type SyncState = 'idle' | 'syncing' | 'success' | 'partial' | 'error';

const RESET_DELAY_MS = 3000;

/**
 * Start a sync, and reflect one that is already running.
 *
 * "Is a sync happening" is now read from the SERVER rather than from this
 * component's own mutation, which is what lets the button reflect a run it did
 * not start — the 9pm schedule, or another tab. A local `isPending` could see
 * neither, and would have offered a button that starts a second run only for the
 * API to refuse it with a 409.
 *
 * The finished-state flash is still local. It is a three-second cosmetic
 * acknowledgement, and the durable reporting lives in the toast and the header
 * badge; deriving it from the server would mean flashing "Synced" on every page
 * navigation for the last run of the day.
 */
export const SyncBrokerButton: React.FC = () => {
  const [flash, setFlash] = useState<SyncState | null>(null);
  const [detail, setDetail] = useState<string | null>(null);
  // The full IBKR message behind a partial run, surfaced on hover.
  const [reason, setReason] = useState<string | null>(null);

  // Track the reset timer and mount status so a state flash never lands after
  // the component unmounts.
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const isMounted = useRef(true);
  // Whether the run currently on screen is one this button watched begin.
  const wasRunning = useRef(false);

  const syncMutation = useSyncBroker();
  const inFlight = useSyncInFlight();
  const { data: status } = useSyncStatus();
  const latest = status?.latest ?? null;

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
      setFlash(null);
      setDetail(null);
      setReason(null);
    }, RESET_DELAY_MS);
  }, []);

  // Report a run when it finishes, whoever started it.
  //
  // Armed only after seeing `running`, for the same reason useSyncRunWatcher is:
  // on mount `latest` is usually a terminal run from hours ago, and flashing
  // "Synced ✓" for that on every page load would be a lie about what just
  // happened.
  useEffect(() => {
    if (inFlight) {
      wasRunning.current = true;
      return;
    }
    if (!wasRunning.current || !latest) return;
    wasRunning.current = false;

    const stored = latest.result;
    const failedQueries = stored?.queries_failed ?? [];

    if (latest.outcome === 'error') {
      setFlash('error');
      setDetail(latest.error ?? 'The sync failed.');
      setReason(latest.error ?? null);
    } else if (latest.outcome === 'partial' || failedQueries.length > 0) {
      // A query that did not return leaves a gap in the data. Reporting that as
      // a plain success is how "no fills" comes to mean "IBKR refused us" --
      // indistinguishable, from the button, from a genuinely quiet day.
      setFlash('partial');
      setDetail(
        failedQueries.length === 1
          ? '1 query unavailable'
          : `${failedQueries.length || 'Some'} queries unavailable`
      );
      setReason(failedQueries.join(' | ') || null);
    } else {
      setFlash('success');
      setReason(null);
      // Distinguish "found new fills" from "already up to date": a run where
      // everything was rejected as a duplicate is a healthy no-op, not a miss.
      setDetail(
        (stored?.trades_created ?? 0) > 0
          ? `${stored?.trades_created} new`
          : (stored?.staged_duplicates ?? 0) > 0
            ? 'up to date'
            : 'no fills'
      );
    }
    scheduleReset();
  }, [inFlight, latest, scheduleReset]);

  const handleSync = useCallback(async () => {
    // Guard against double-submits even if the disabled attribute is bypassed.
    // The API refuses a concurrent run with a 409 regardless.
    if (inFlight) return;

    setFlash(null);
    setDetail(null);

    try {
      const started = await syncMutation.mutateAsync();
      if (!isMounted.current) return;

      // Handed off: the polled status drives the button from here, and the
      // effect above reports the outcome when it lands.
      if (started.kind === 'started') {
        wasRunning.current = true;
        return;
      }

      // The synchronous fallback -- the server could not record the run, so it
      // finished inline and there is nothing to poll for.
      const result = started.result;
      if (result.queries_failed.length > 0) {
        setFlash('partial');
        setDetail(
          result.queries_failed.length === 1
            ? '1 query unavailable'
            : `${result.queries_failed.length} queries unavailable`
        );
        setReason(result.queries_failed.join(' | '));
      } else {
        setFlash('success');
        setReason(null);
        setDetail(
          result.trades_created > 0
            ? `${result.trades_created} new`
            : result.staged_duplicates > 0
              ? 'up to date'
              : 'no fills'
        );
      }
      scheduleReset();
    } catch (err) {
      console.error('Broker ingest failed:', err);
      if (!isMounted.current) return;
      setFlash('error');
      setDetail(err instanceof Error ? err.message : null);
      setReason(err instanceof Error ? err.message : null);
      scheduleReset();
    }
  }, [inFlight, scheduleReset, syncMutation]);

  // The server's view wins over the flash: a run in flight is a fact, where the
  // flash is a fading acknowledgement of the previous one.
  const state: SyncState = inFlight
    ? 'syncing'
    : syncMutation.isPending
      ? 'syncing'
      : (flash ?? 'idle');

  const isSyncing = state === 'syncing';

  const label =
    state === 'syncing'
      ? 'Syncing Broker...'
      : state === 'success'
        ? 'Synced ✓'
        : state === 'partial'
          ? 'Partial sync'
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
    // Amber, not green: the run completed but the picture is incomplete.
    partial:
      'bg-amber-500/10 border-amber-500/40 text-amber-300 focus-visible:ring-amber-400',
    error:
      'bg-loss-glow border-loss-border text-loss shadow-loss-glow focus-visible:ring-loss',
  };

  const icon =
    state === 'syncing' ? (
      <RefreshCw className="h-3.5 w-3.5 animate-spin" />
    ) : state === 'success' ? (
      <Check className="h-3.5 w-3.5" />
    ) : state === 'partial' ? (
      <AlertTriangle className="h-3.5 w-3.5" />
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
      title={reason ?? detail ?? 'Pull the latest executions from IBKR'}
      className={`${base} ${stateStyles[state]}`}
    >
      {/* Top hairline highlight for the glass effect */}
      <span
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 top-0 h-px rounded-t-lg bg-gradient-to-r from-transparent via-white/10 to-transparent"
      />
      {icon}
      <span className="whitespace-nowrap">{label}</span>
      {(state === 'success' || state === 'partial') && detail && (
        <span className="font-mono text-[10px] opacity-70">{detail}</span>
      )}
    </button>
  );
};

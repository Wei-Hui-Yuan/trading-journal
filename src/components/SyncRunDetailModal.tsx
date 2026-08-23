'use client';

import React from 'react';
import { createPortal } from 'react-dom';
import { RefreshCw, X } from 'lucide-react';

import { useLatestSyncRun } from '@/hooks/useTradeInbox';
import { SYNC_TONE, SyncRunDetail } from './SyncRunDetail';

/**
 * Absolute, with the date, because this run may not be from today.
 *
 * In ET, and labelled as such, because the badge that opens this panel reads
 * "12:44 ET" -- rendering the same run in the browser's own zone here would
 * put two different clock times on one sync, which is precisely the confusion
 * sharing `useLatestSyncRun` was meant to prevent.
 */
const stampFormatter = new Intl.DateTimeFormat('en-US', {
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
  timeZone: 'America/New_York',
});

const stamp = (iso: string) => `${stampFormatter.format(new Date(iso))} ET`;

/**
 * The last sync, on demand.
 *
 * The toast reports a run as it finishes and then goes away for good, which
 * left two cases with nothing to look at. A dismissed toast could not be
 * reopened; and a run that finished while the browser was closed was never
 * toasted at all, because `useSyncRunWatcher` only arms after it has SEEN a
 * run go from `running` to finished. An overnight scheduled sync therefore
 * left its full result sitting in the query cache, fetched and unreachable
 * -- exactly what `sync_runs.result` was persisted for.
 *
 * Reads `useLatestSyncRun`, the same source the badge renders from, so the
 * figures here always describe the run the badge is naming.
 */
export const SyncRunDetailModal: React.FC<{ open: boolean; onClose: () => void }> = ({
  open,
  onClose,
}) => {
  const run = useLatestSyncRun();

  React.useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  // No `mounted` effect to defer the first render, unlike the older modals
  // here: that pattern exists so a portal is never built during SSR, and this
  // one cannot be. `open` is driven solely by a client `useState(false)`, so
  // the server and the first client render both take this branch and agree.
  // Guarding on `document` states the actual requirement -- somewhere to
  // portal INTO -- rather than approximating it with a render cycle.
  if (!open || typeof document === 'undefined') return null;

  const tone = run ? SYNC_TONE[run.outcome] : null;

  return createPortal(
    <div
      className="fixed inset-0 z-[120] flex items-start justify-center overflow-y-auto p-4 py-10"
      role="dialog"
      aria-modal="true"
      aria-label="Last sync detail"
    >
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" onClick={onClose} />

      <div className="relative w-full max-w-lg rounded-xl border border-obsidian-border bg-obsidian-card shadow-2xl">
        <div
          className={`flex items-start justify-between border-b border-obsidian-border px-5 py-4 ${
            tone ? tone.bg : ''
          }`}
        >
          <div className="flex items-start gap-2">
            {tone ? (
              <tone.Icon className={`mt-px h-4 w-4 shrink-0 ${tone.text}`} />
            ) : (
              <RefreshCw className="mt-px h-4 w-4 shrink-0 text-slate-300" />
            )}
            <div>
              <h2
                className={`text-sm font-semibold tracking-wide ${
                  tone ? tone.text : 'text-slate-100'
                }`}
              >
                {tone ? tone.title : 'IBKR sync'}
              </h2>
              {run && (
                <p className="mt-0.5 text-[11px] text-obsidian-muted">
                  {stamp(run.at)}
                  {/* Which is not cosmetic: "the schedule ran and found
                      nothing" and "nothing has run since you pressed the
                      button" are the two states worth telling apart, and a
                      bare timestamp tells neither. */}
                  {' · '}
                  {run.trigger === 'cron'
                    ? 'scheduled'
                    : run.fromMemory
                      ? 'this tab'
                      : 'manual'}
                  {run.status !== null && ` · HTTP ${run.status}`}
                </p>
              )}
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded-lg p-1 text-obsidian-muted transition-colors hover:bg-obsidian-bg hover:text-slate-200"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {run ? (
          <SyncRunDetail result={run.result} summary={run.summary} />
        ) : (
          /* Says "not yet", not "never" -- an in-flight run is deliberately
             excluded upstream, and claiming nothing had happened while a sync
             was mid-flight would be the one wrong answer. */
          <div className="px-5 py-8 text-center">
            <p className="text-xs text-slate-300">Nothing to report yet.</p>
            <p className="mt-1 text-[11px] text-obsidian-muted">
              No sync has finished — either none has run, or one is in flight
              right now.
            </p>
          </div>
        )}
      </div>
    </div>,
    document.body
  );
};

export default SyncRunDetailModal;

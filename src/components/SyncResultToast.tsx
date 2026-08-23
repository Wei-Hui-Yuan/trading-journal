'use client';

import React from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';

import { useAcknowledgeSync, useLastSync } from '@/hooks/useTradeInbox';
import { SYNC_TONE, SyncRunDetail } from './SyncRunDetail';

/**
 * What the last sync actually did, in full.
 *
 * The button could only ever say "Synced ✓" — a run that imported nothing
 * because IBKR throttled it looked identical to a genuinely quiet day, and a
 * run that skipped fills the user had deleted said nothing at all. Every
 * figure the ingest returns is reported here, because the interesting ones are
 * the zeros: "0 new, 45 already in the ledger" is the sentence that proves the
 * sync is idempotent.
 *
 * Stays until dismissed rather than fading. A summary you have to catch within
 * three seconds is decoration.
 *
 * The figures themselves live in `SyncRunDetail`, shared with the badge's
 * detail modal. What is left here is what makes this a TOAST rather than a
 * panel: it appears unbidden when a run this tab started finishes, and it can
 * be dismissed. Reopening a dismissed one is the modal's job.
 */
export const SyncResultToast: React.FC = () => {
  const lastSync = useLastSync();
  const acknowledge = useAcknowledgeSync();
  const [mounted, setMounted] = React.useState(false);

  React.useEffect(() => setMounted(true), []);

  if (!mounted || !lastSync || lastSync.acknowledged) return null;

  const { outcome, result, summary, status } = lastSync;
  const tone = SYNC_TONE[outcome];

  return createPortal(
    <div
      role="status"
      aria-live="polite"
      className="fixed bottom-4 right-4 z-[120] w-[22rem] max-w-[calc(100vw-2rem)]"
    >
      <div
        className={`rounded-xl border ${tone.border} bg-obsidian-card shadow-2xl overflow-hidden`}
      >
        <div className={`flex items-start gap-2 px-4 py-3 ${tone.bg}`}>
          <tone.Icon className={`mt-px h-4 w-4 shrink-0 ${tone.text}`} />
          <div className="flex-1">
            <p className={`text-sm font-semibold ${tone.text}`}>{tone.title}</p>
            <p className="mt-0.5 text-[11px] text-obsidian-muted">
              {status !== null ? `HTTP ${status}` : 'No response from the server'}
              {' · '}
              {new Date(lastSync.at).toLocaleTimeString()}
            </p>
          </div>
          <button
            type="button"
            onClick={acknowledge}
            aria-label="Dismiss sync summary"
            className="rounded p-0.5 text-obsidian-muted transition-colors hover:text-slate-200"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>

        <SyncRunDetail result={result} summary={summary} />
      </div>
    </div>,
    document.body
  );
};

export default SyncResultToast;

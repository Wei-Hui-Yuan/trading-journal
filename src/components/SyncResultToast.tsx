'use client';

import React from 'react';
import { createPortal } from 'react-dom';
import { AlertTriangle, Check, Clock, X, XCircle } from 'lucide-react';

import { useAcknowledgeSync, useLastSync } from '@/hooks/useTradeInbox';

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
 */
export const SyncResultToast: React.FC = () => {
  const lastSync = useLastSync();
  const acknowledge = useAcknowledgeSync();
  const [mounted, setMounted] = React.useState(false);

  React.useEffect(() => setMounted(true), []);

  if (!mounted || !lastSync || lastSync.acknowledged) return null;

  const { outcome, result, summary, status } = lastSync;

  const tone =
    outcome === 'success'
      ? { border: 'border-win-border', bg: 'bg-win-glow', text: 'text-win', Icon: Check }
      : outcome === 'partial'
        ? {
            border: 'border-amber-500/40',
            bg: 'bg-amber-500/10',
            text: 'text-amber-300',
            Icon: AlertTriangle,
          }
        : {
            border: 'border-loss-border',
            bg: 'bg-loss-glow',
            text: 'text-loss',
            Icon: XCircle,
          };

  const title =
    outcome === 'success'
      ? 'Sync complete'
      : outcome === 'partial'
        ? 'Sync incomplete'
        : 'Sync failed';

  // Only the figures that carry information -- which turned out to include
  // `executions_parsed`, dropped here originally as "describing the staging
  // table rather than the ledger". On a re-sync where nothing is new, every
  // ledger-side counter is legitimately zero and the staging figures are the
  // only evidence the Flex query returned anything at all.
  //
  // "Already in the ledger" sums both duplicate counters on purpose. A fill
  // stopped at staging and one stopped at the trades insert are the same fact
  // to the reader: the broker re-sent it, and the journal already had it.
  // Reporting only the second reads as 0 whenever the first caught everything.
  const rows: { label: string; value: number; hint?: string }[] = result
    ? [
        {
          // Without this the panel reads 0 across the board on any re-sync,
          // which is indistinguishable from IBKR returning nothing at all --
          // and telling those two apart is the whole reason to look.
          label: 'Executions IBKR returned',
          value: result.executions_parsed,
          hint: 'Rows in the Flex report. Zero means the query came back empty.',
        },
        { label: 'New fills imported', value: result.trades_created },
        {
          label: 'Already in the ledger',
          value: result.staged_duplicates + result.trades_duplicates,
          hint: 'Recognised by broker id and skipped — this is idempotency working.',
        },
        {
          label: 'Suppressed, skipped',
          value: result.suppressed_skipped ?? 0,
          hint: 'Fills you deleted. IBKR re-sent them; the tombstone held.',
        },
        {
          label: 'Trade plans attached',
          value: result.plans_attached ?? 0,
          hint: 'A plan you wrote beforehand met the fill it was waiting for.',
        },
        {
          label: 'Non-tradeable rows',
          value: result.skipped_non_tradeable,
          hint: 'Currency conversions and similar, which are not positions.',
        },
        { label: 'Round trips matched', value: result.positions_matched },
      ]
    : [];

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
            <p className={`text-sm font-semibold ${tone.text}`}>{title}</p>
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

        <div className="px-4 py-3">
          {result ? (
            <>
              <dl className="space-y-1.5">
                {rows.map((row) => (
                  <div key={row.label} className="flex items-baseline justify-between gap-3">
                    <dt
                      className="text-[11px] text-obsidian-muted"
                      title={row.hint}
                    >
                      {row.label}
                    </dt>
                    <dd
                      className={`font-mono text-xs ${
                        row.value > 0 ? 'text-slate-200' : 'text-obsidian-muted'
                      }`}
                    >
                      {row.value}
                    </dd>
                  </div>
                ))}
              </dl>

              {result.symbols_touched.length > 0 && (
                <p className="mt-2 text-[10px] text-obsidian-muted">
                  Tickers rebuilt: {result.symbols_touched.join(', ')}
                </p>
              )}

              {/* Rare, and never something to discover later from a changed
                  headline. A fill dated earlier than ones already stored
                  re-partitions FIFO for that ticker, so round trips closed by
                  an earlier run can stop existing — taking their P&L out of
                  every statistic, and their review with them. */}
              {(result.positions_removed ?? 0) > 0 && (
                <p className="mt-3 rounded-lg border border-loss/30 bg-loss/5 px-2.5 py-2 text-[11px] leading-relaxed text-loss">
                  <span className="font-semibold">
                    {result.positions_removed} closed round trip
                    {result.positions_removed === 1 ? '' : 's'} no longer
                    match{result.positions_removed === 1 ? 'es' : ''} and{' '}
                    {result.positions_removed === 1 ? 'was' : 'were'} removed.
                  </span>{' '}
                  A fill arrived dated earlier than ones already recorded, which
                  re-pairs that ticker&apos;s trades. Your P&amp;L and trade
                  count have changed accordingly.
                  {(result.reviews_discarded ?? 0) > 0 && (
                    <>
                      {' '}
                      <span className="font-semibold">
                        {result.reviews_discarded} review
                        {result.reviews_discarded === 1 ? '' : 's'}
                      </span>{' '}
                      could not be carried over.
                    </>
                  )}
                </p>
              )}

              {/* Called out rather than left as a number in the list: this is
                  the one outcome that changed a trade's recorded intent, and
                  it is worth checking the sync matched the plan you meant. */}
              {(result.plans_attached ?? 0) > 0 && (
                <p className="mt-2 rounded-lg border border-amber-500/25 bg-amber-500/5 px-2.5 py-2 text-[11px] leading-relaxed text-amber-200/90">
                  <span className="font-semibold">
                    {result.plans_attached} trade plan
                    {result.plans_attached === 1 ? '' : 's'} attached.
                  </span>{' '}
                  Your planned stop and target are now on the fill. Check it in
                  the Journal if more than one plan could have matched.
                </p>
              )}
            </>
          ) : (
            <p className="text-[11px] text-loss">{summary}</p>
          )}

          {/* The actionable case. Throttling clears on its own, so the advice
              is "wait", not "check your token" — and saying which it is, is the
              entire reason the backend classifies the failure. */}
          {result && result.queries_failed.length > 0 && (
            <div className="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/5 px-2.5 py-2">
              <div className="flex items-start gap-1.5">
                <Clock className="mt-px h-3 w-3 shrink-0 text-amber-400" />
                <div className="text-[11px] leading-relaxed text-amber-200/90">
                  {result.rate_limited ? (
                    <>
                      <span className="font-semibold">
                        {result.queries_failed.length} Flex quer
                        {result.queries_failed.length === 1 ? 'y was' : 'ies were'}{' '}
                        rate-limited by IBKR.
                      </span>{' '}
                      Try syncing again in a few minutes — this clears on its own.
                      Some fills may be missing until then.
                    </>
                  ) : (
                    <>
                      <span className="font-semibold">
                        {result.queries_failed.length} quer
                        {result.queries_failed.length === 1 ? 'y' : 'ies'} did not
                        return.
                      </span>{' '}
                      This will not fix itself — check the query id and token.
                    </>
                  )}
                </div>
              </div>
              <ul className="mt-1.5 space-y-0.5 pl-4.5">
                {result.queries_failed.map((failure) => (
                  <li
                    key={failure}
                    className="truncate font-mono text-[10px] text-amber-200/60"
                    title={failure}
                  >
                    {failure}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </div>,
    document.body
  );
};

export default SyncResultToast;

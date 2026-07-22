'use client';

import React, { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { AlertCircle, GhostIcon, Loader2, Loader, RotateCcw, X } from 'lucide-react';

import { useSuppressedExecutions, useUnsuppressTrade } from '@/hooks/useTradeInbox';

interface SuppressedFillsModalProps {
  open: boolean;
  onClose: () => void;
}

const when = new Intl.DateTimeFormat('en-US', {
  year: 'numeric',
  month: 'short',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
  timeZone: 'America/New_York',
});

/** Trailing zeros on a fractional size are noise; 0.25 should read as 0.25. */
const qty = (n: number) => Number(n.toFixed(8)).toString();

/**
 * Broker fills the journal is deliberately ignoring, and how to stop.
 *
 * Deleting a broker fill writes a tombstone, because ingest is idempotent
 * through ON CONFLICT DO NOTHING — which skips rows that still exist, and a
 * deleted row does not, so without one every sync would resurrect it. That
 * made deletion permanent and, until this screen, invisible: a fill deleted by
 * mistake was gone with nothing on any page to say so.
 */
export const SuppressedFillsModal: React.FC<SuppressedFillsModalProps> = ({
  open,
  onClose,
}) => {
  const query = useSuppressedExecutions();
  const unsuppress = useUnsuppressTrade();
  const [mounted, setMounted] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setMounted(true), []);

  useEffect(() => {
    if (open) {
      setNotice(null);
      setError(null);
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open || !mounted) return null;

  const rows = query.data ?? [];

  return createPortal(
    <div
      className="fixed inset-0 z-[110] flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Suppressed broker fills"
    >
      <div
        className="absolute inset-0 bg-black/70 backdrop-blur-sm"
        onClick={onClose}
      />

      <div className="relative w-full max-w-3xl rounded-xl border border-obsidian-border bg-obsidian-card shadow-2xl">
        <div className="flex items-center justify-between border-b border-obsidian-border px-5 py-4">
          <div className="flex items-center gap-2">
            <GhostIcon className="h-4 w-4 text-amber-400" />
            <h2 className="text-sm font-semibold tracking-wide text-slate-100">
              SUPPRESSED FILLS
            </h2>
            {!query.isPending && (
              <span className="font-mono text-[10px] text-obsidian-muted">
                {rows.length}
              </span>
            )}
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

        <div className="max-h-[70vh] overflow-y-auto px-5 py-4">
          <p className="mb-4 text-[11px] leading-relaxed text-obsidian-muted">
            Broker fills you deleted. Every sync skips these — without that, the
            next sync would simply add them back and the delete would undo
            itself. Lifting a suppression does{' '}
            <span className="text-slate-300">not</span> restore the fill on its
            own: it returns the next time a sync covers that date, so an old
            fill also needs a Flex query window that reaches back far enough.
          </p>

          {query.isPending && (
            <div className="flex items-center justify-center py-10 text-obsidian-muted">
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              <span className="text-xs">Loading…</span>
            </div>
          )}

          {query.isError && (
            <div className="flex items-start py-4 text-xs text-loss">
              <AlertCircle className="mr-1.5 h-4 w-4 shrink-0" />
              <span>
                {query.error instanceof Error
                  ? query.error.message
                  : 'Could not load suppressed fills.'}
              </span>
            </div>
          )}

          {!query.isPending && !query.isError && rows.length === 0 && (
            <div className="py-10 text-center">
              <p className="text-sm text-slate-300">Nothing is suppressed.</p>
              <p className="mt-1 text-[11px] text-obsidian-muted">
                Every broker fill IBKR sends is eligible for import.
              </p>
            </div>
          )}

          {rows.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[620px] text-left text-[11px]">
                <thead className="text-obsidian-muted">
                  <tr className="border-b border-obsidian-border">
                    <th className="pb-2 font-normal">Ticker</th>
                    <th className="pb-2 font-normal">Side</th>
                    <th className="pb-2 font-normal">Qty</th>
                    <th className="pb-2 font-normal">Price</th>
                    <th className="pb-2 font-normal">Executed</th>
                    <th className="pb-2 font-normal">Why</th>
                    <th className="pb-2 text-right font-normal">Action</th>
                  </tr>
                </thead>
                <tbody className="font-mono text-slate-300">
                  {rows.map((row) => (
                    <tr
                      key={row.ibkr_exec_id}
                      className="border-b border-obsidian-border/60"
                    >
                      <td className="py-2 font-sans font-semibold text-slate-200">
                        {row.ticker ?? '—'}
                      </td>
                      <td className="py-2">
                        {row.direction ? (
                          <span
                            className={
                              row.direction === 'BUY' ? 'text-win' : 'text-loss'
                            }
                          >
                            {row.direction}
                          </span>
                        ) : (
                          '—'
                        )}
                      </td>
                      {/* Em dash, not 0: these columns did not exist when the
                          earliest tombstones were written, and the fill they
                          name is deleted, so there is nothing to backfill
                          from. "Not recorded" is the honest answer. */}
                      <td className="py-2">
                        {row.quantity === null ? '—' : qty(row.quantity)}
                      </td>
                      <td className="py-2">{row.price === null ? '—' : row.price}</td>
                      <td className="py-2 text-obsidian-muted">
                        {row.executed_at ? when.format(new Date(row.executed_at)) : '—'}
                      </td>
                      <td
                        className="max-w-[10rem] truncate py-2 font-sans text-obsidian-muted"
                        title={`${row.reason ?? 'No reason recorded'} · ${row.ibkr_exec_id}`}
                      >
                        {row.reason ?? '—'}
                      </td>
                      <td className="py-2 text-right">
                        <button
                          type="button"
                          onClick={() => {
                            setNotice(null);
                            setError(null);
                            unsuppress.mutate(row.ibkr_exec_id, {
                              onSuccess: (result) =>
                                setNotice(
                                  `${result.ticker ?? result.ibkr_exec_id} un-suppressed. ` +
                                    'It will return the next time a sync covers that date — nothing has changed yet.'
                                ),
                              onError: (err) => setError(err.message),
                            });
                          }}
                          disabled={unsuppress.isPending}
                          className="inline-flex items-center gap-1 rounded border border-obsidian-border px-2 py-1 font-sans text-[10px] text-obsidian-muted transition-colors hover:border-slate-600 hover:text-slate-200 disabled:opacity-50"
                          title="Allow future syncs to import this fill again"
                        >
                          {unsuppress.isPending &&
                          unsuppress.variables === row.ibkr_exec_id ? (
                            <Loader className="h-3 w-3 animate-spin" />
                          ) : (
                            <RotateCcw className="h-3 w-3" />
                          )}
                          Un-suppress
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {notice && (
            <div className="mt-4 rounded-lg border border-obsidian-border bg-obsidian-bg/60 px-3 py-2 text-[11px] text-slate-300">
              {notice}
            </div>
          )}
          {error && (
            <div className="mt-4 flex items-start text-xs text-loss">
              <AlertCircle className="mr-1.5 mt-px h-3.5 w-3.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}
        </div>
      </div>
    </div>,
    document.body
  );
};

export default SuppressedFillsModal;

'use client';

import React, { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { AlertTriangle, CheckCircle2, Loader2, RefreshCw, ShieldCheck, X } from 'lucide-react';

import { useLastAudit, useRunDataAudit } from '@/hooks/useDataAudit';
import type { AuditCheck, AuditStatus } from '@/types/api';

const TONE: Record<AuditStatus, { dot: string; text: string; border: string; label: string }> = {
  clean: { dot: 'bg-win', text: 'text-win', border: 'border-win-border', label: 'Clean' },
  attention: {
    dot: 'bg-amber-400',
    text: 'text-amber-300',
    border: 'border-amber-500/40',
    label: 'Attention',
  },
  critical: { dot: 'bg-loss', text: 'text-loss', border: 'border-loss/40', label: 'Critical' },
};

/** `pnl_drift` -> `Pnl drift`. The keys are already written to be read. */
const humanise = (key: string) =>
  key.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());

/** Counts that are money, not tallies -- shown signed and to the cent. */
const MONEY_KEYS = new Set([
  'pnl_in_stale_rows',
  'pnl_drift',
  'legs_net_pnl',
  'legs_gross_pnl',
  'banked_from_open_positions',
  'round_trip_rows_report',
  'legs_vs_fifo',
  'legs_vs_round_trips',
]);

const formatCount = (key: string, value: number) =>
  MONEY_KEYS.has(key)
    ? `${value >= 0 ? '+' : '−'}${Math.abs(value).toLocaleString('en-US', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })}`
    : value.toLocaleString('en-US');

const CheckRow: React.FC<{ check: AuditCheck }> = ({ check }) => {
  const tone = TONE[check.status];
  return (
    <div className={`rounded-lg border ${tone.border} bg-obsidian-bg/50 p-3`}>
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <span className={`h-2 w-2 shrink-0 rounded-full ${tone.dot}`} />
          <span className="text-xs font-semibold text-slate-100">{check.label}</span>
        </div>
        <span className={`text-[10px] font-mono uppercase tracking-wider ${tone.text}`}>
          {tone.label}
        </span>
      </div>

      <p className="mt-1.5 pl-4 text-[11px] leading-relaxed text-obsidian-muted">
        {check.headline}
      </p>

      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 pl-4">
        {Object.entries(check.counts).map(([key, value]) => (
          <span key={key} className="font-mono text-[10px] text-slate-500">
            {humanise(key)}{' '}
            <span className={value !== 0 && !MONEY_KEYS.has(key) ? 'text-slate-300' : 'text-slate-400'}>
              {formatCount(key, value)}
            </span>
          </span>
        ))}
      </div>

      {check.items.length > 0 && (
        <ul className="mt-2 space-y-1 border-t border-obsidian-border pl-4 pt-2">
          {check.items.map((item, i) => (
            <li key={i} className="font-mono text-[10px] leading-relaxed text-slate-400">
              <span className="text-slate-200">{item.ticker}</span>{' '}
              <span className={tone.text}>{item.kind}</span> — {item.detail}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
};

/**
 * What the audit found, and the button that runs it.
 *
 * Scoped wording throughout: this verifies FIFO and broker reconciliation,
 * not "system health" in general. An audit page that overstates its coverage
 * is worse than none -- it earns a trust its assertions do not.
 */
export const DataHealthModal: React.FC<{ open: boolean; onClose: () => void }> = ({
  open,
  onClose,
}) => {
  const [mounted, setMounted] = useState(false);
  const audit = useLastAudit();
  const run = useRunDataAudit();

  useEffect(() => setMounted(true), []);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open || !mounted) return null;

  const tone = audit ? TONE[audit.status] : null;

  return createPortal(
    <div
      className="fixed inset-0 z-[120] flex items-start justify-center overflow-y-auto p-4 py-10"
      role="dialog"
      aria-modal="true"
      aria-label="Data health audit"
    >
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" onClick={onClose} />

      <div className="relative w-full max-w-2xl rounded-xl border border-obsidian-border bg-obsidian-card shadow-2xl">
        <div className="flex items-start justify-between border-b border-obsidian-border px-5 py-4">
          <div className="flex items-center gap-2">
            <ShieldCheck className="h-4 w-4 text-slate-300" />
            <div>
              <h2 className="text-sm font-semibold tracking-wide text-slate-100">
                Data health audit
              </h2>
              <p className="text-[11px] text-obsidian-muted">
                FIFO and broker reconciliation — does stored state still agree with the fills?
              </p>
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

        <div className="space-y-3 px-5 py-4">
          {!audit && !run.isPending && (
            <div className="rounded-lg border border-dashed border-obsidian-border px-4 py-8 text-center">
              <p className="text-xs text-slate-300">Not run yet this session.</p>
              <p className="mt-1 text-[11px] text-obsidian-muted">
                Nothing is remembered across a reload — a verdict from earlier could not
                vouch for a ledger that has changed since.
              </p>
            </div>
          )}

          {run.isPending && (
            <div className="flex items-center justify-center gap-2 px-4 py-8 text-xs text-obsidian-muted">
              <Loader2 className="h-4 w-4 animate-spin" />
              Rebuilding every ticker from its fills…
            </div>
          )}

          {run.isError && (
            <div className="flex items-start gap-2 rounded-lg border border-loss/30 bg-loss/5 p-3 text-[11px] text-loss">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{run.error.message}</span>
            </div>
          )}

          {audit && !run.isPending && (
            <>
              <div
                className={`flex flex-wrap items-center justify-between gap-2 rounded-lg border ${tone!.border} bg-obsidian-bg/50 px-3 py-2`}
              >
                <div className="flex items-center gap-2">
                  {audit.status === 'clean' ? (
                    <CheckCircle2 className={`h-4 w-4 ${tone!.text}`} />
                  ) : (
                    <AlertTriangle className={`h-4 w-4 ${tone!.text}`} />
                  )}
                  <span className={`text-xs font-semibold ${tone!.text}`}>
                    {audit.status === 'clean'
                      ? 'Everything checked ties out'
                      : audit.status === 'attention'
                        ? 'Worth a look — no displayed figure is known to be wrong'
                        : 'A displayed figure disagrees with the fills underneath it'}
                  </span>
                </div>
                <span className="font-mono text-[10px] text-slate-500">
                  {audit.tickers_checked} tickers · {audit.duration_ms}ms ·{' '}
                  {new Date(audit.generated_at).toLocaleTimeString()}
                </span>
              </div>

              {audit.checks.map((check) => (
                <CheckRow key={check.key} check={check} />
              ))}

              {audit.status === 'critical' && (
                <p className="text-[11px] leading-relaxed text-obsidian-muted">
                  A cold rebuild disagrees with what is stored. `POST /api/rematch` rewrites
                  the round trips from the fills and is safe to run — it is idempotent, and
                  only removes round trips FIFO no longer produces.
                </p>
              )}
            </>
          )}
        </div>

        <div className="flex items-center justify-between gap-2 border-t border-obsidian-border px-5 py-3">
          <span className="text-[10px] text-slate-600">
            Read-only. Nothing here writes to the ledger.
          </span>
          <button
            type="button"
            onClick={() => run.mutate()}
            disabled={run.isPending}
            className="inline-flex items-center gap-1.5 rounded-lg border border-obsidian-border bg-obsidian-bg px-3.5 py-2 text-xs font-medium text-slate-300 transition-colors hover:border-slate-600 hover:text-slate-100 disabled:opacity-50"
          >
            {run.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <RefreshCw className="h-3.5 w-3.5" />
            )}
            {audit ? 'Run again' : 'Run audit'}
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
};

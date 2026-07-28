'use client';

import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { AlertCircle, Check, Loader2, Wrench, X } from 'lucide-react';

import { useCreateManualTrade } from '@/hooks/useTradeInbox';
import type { TradeSide } from '@/types/api';

interface RepairFillModalProps {
  open: boolean;
  onClose: () => void;
  /**
   * Ticker to open with — this is only ever reached from a position that is
   * already on screen. Still editable, because the symbol is a starting point
   * rather than a lock.
   */
  presetSymbol?: string;
}

interface FormState {
  symbol: string;
  side: TradeSide;
  quantity: string;
  price: string;
  commission: string;
  executionTime: string;
}

/** '' / whitespace / unparseable -> null, so the API never receives NaN. */
function toNullableNumber(raw: string): number | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
}

/**
 * "now" formatted for a datetime-local input, in US market time.
 *
 * The field is labelled ET because the backend reads a naive timestamp as
 * America/New_York and the heatmap buckets sessions the same way. Defaulting
 * to browser-local would silently mis-file trades for anyone outside ET.
 */
function nowInMarketTz(): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(new Date());

  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? '00';
  // en-CA yields ISO-ordered parts; hour can come back as "24" at midnight.
  const hour = get('hour') === '24' ? '00' : get('hour');
  return `${get('year')}-${get('month')}-${get('day')}T${hour}:${get('minute')}`;
}

const blankForm = (): FormState => ({
  symbol: '',
  side: 'BUY',
  quantity: '',
  price: '',
  commission: '',
  executionTime: nowInMarketTz(),
});

/**
 * Add an execution IBKR never sent.
 *
 * This is what remains of the old Manual Log, and it is deliberately much
 * smaller. General hand-logging is what made duplication possible: `trades`
 * deduplicates on the broker's execution id, so a hand-typed fill and IBKR's
 * copy of the same trade could never recognise each other, and logging a trade
 * here before syncing produced two rows for one real execution.
 *
 * Planning now happens in Create Plan, which writes to a different table
 * entirely. The only reason left to type an execution into the ledger is that
 * the broker feed is demonstrably missing one — which is why this is reachable
 * only from a position already on screen, and why there is no plan, sizing or
 * thesis here. Those belong to the plan; this is repairing a fact.
 *
 * Rows written here carry a REPAIR- prefix and are labelled in the ledger. A
 * hand-typed price is an assertion, and should look like one next to figures
 * the broker vouched for.
 */
export function RepairFillModal({
  open,
  onClose,
  presetSymbol,
}: RepairFillModalProps) {
  const [form, setForm] = useState<FormState>(blankForm);
  const [error, setError] = useState<string | null>(null);
  const [savedSummary, setSavedSummary] = useState<string | null>(null);
  const [mounted, setMounted] = useState(false);
  const symbolRef = useRef<HTMLInputElement>(null);

  const mutation = useCreateManualTrade();

  useEffect(() => setMounted(true), []);

  useEffect(() => {
    if (open) {
      setForm({ ...blankForm(), symbol: (presetSymbol ?? '').toUpperCase() });
      setError(null);
      setSavedSummary(null);
      window.setTimeout(() => symbolRef.current?.focus(), 0);
    }
  }, [open, presetSymbol]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !mutation.isPending) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose, mutation.isPending]);

  if (!open || !mounted) return null;

  const patch = (patchObj: Partial<FormState>) => {
    setForm((prev) => ({ ...prev, ...patchObj }));
    setError(null);
    setSavedSummary(null);
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();

    const symbol = form.symbol.trim().toUpperCase();
    const quantity = toNullableNumber(form.quantity);
    const price = toNullableNumber(form.price);

    if (!symbol) return setError('Ticker is required.');
    if (symbol.length > 10) return setError('Ticker must be 10 characters or fewer.');
    if (quantity === null || quantity <= 0)
      return setError('Quantity must be a positive number.');
    // Required here, unlike in a plan: this row asserts that an execution
    // happened, and an execution without a price is not a fact about anything.
    if (price === null || price <= 0)
      return setError('Fill price must be greater than zero.');

    mutation.mutate(
      {
        symbol,
        side: form.side,
        quantity,
        price,
        commission: toNullableNumber(form.commission) ?? 0,
        // Sent without an offset; the backend anchors it to America/New_York.
        execution_time: form.executionTime ? `${form.executionTime}:00` : null,
      },
      {
        onSuccess: (result) => {
          const base =
            result.positions_created > 0
              ? `Added. ${result.positions_created} position${
                  result.positions_created === 1 ? '' : 's'
                } closed by FIFO matching.`
              : `Added. ${result.open_quantity} share${
                  result.open_quantity === 1 ? '' : 's'
                } open on ${result.ticker}.`;

          // A repair fill is backdated by definition, so it can re-pair
          // trades that were already matched and dissolve round trips closed
          // by an earlier run. Held on screen longer when that happens: it
          // moves P&L and can cost a review, which is not something to
          // notice later from a changed total.
          const removed = result.positions_removed ?? 0;
          const reviews = result.reviews_discarded ?? 0;
          const extra =
            removed > 0
              ? ` ${removed} previously closed round trip${
                  removed === 1 ? '' : 's'
                } no longer match${removed === 1 ? 'es' : ''} and ${
                  removed === 1 ? 'was' : 'were'
                } removed${
                  reviews > 0
                    ? `, including ${reviews} with a review that could not be carried over`
                    : ''
                }.`
              : '';

          setSavedSummary(base + extra);
          window.setTimeout(onClose, removed > 0 ? 6000 : 1400);
        },
        onError: (err) => setError(err.message),
      }
    );
  };

  const fieldClass =
    'w-full rounded-lg bg-obsidian-bg border border-obsidian-border px-3 py-2 text-sm text-slate-200 ' +
    'placeholder:text-obsidian-muted focus:outline-none focus:border-slate-600 transition-colors ' +
    'disabled:opacity-50';

  const isSaving = mutation.isPending;

  return createPortal(
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Add a missing broker fill"
    >
      <div
        className="absolute inset-0 bg-black/70 backdrop-blur-sm"
        onClick={() => !isSaving && onClose()}
      />

      <div className="relative w-full max-w-md rounded-xl border border-obsidian-border bg-obsidian-card shadow-2xl">
        <div className="flex items-center justify-between border-b border-obsidian-border px-5 py-4">
          <div className="flex items-center gap-2">
            <Wrench className="h-4 w-4 text-slate-400" />
            <div>
              <h2 className="text-sm font-semibold tracking-wide text-slate-100">
                ADD MISSING FILL
              </h2>
              <p className="text-[10px] text-obsidian-muted">
                For an execution IBKR never sent
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={isSaving}
            aria-label="Close"
            className="rounded-lg p-1 text-obsidian-muted transition-colors hover:bg-obsidian-bg hover:text-slate-200 disabled:opacity-50"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4 px-5 py-5">
          {/* Said plainly, because the failure it prevents is silent: a fill
              added here for a trade the broker WILL report creates exactly the
              duplicate that plans were introduced to eliminate. */}
          <div className="rounded-lg border border-amber-500/25 bg-amber-500/5 px-3 py-2.5">
            <p className="text-[11px] leading-relaxed text-amber-200/80">
              Only use this when the broker feed is missing an execution you
              know happened. If IBKR will report this trade, let the sync bring
              it — adding it here would leave two rows for one fill.
            </p>
            <p className="mt-1.5 text-[11px] leading-relaxed text-obsidian-muted">
              Planning a trade you have not entered yet?{' '}
              <span className="text-slate-300">Create Trade Plan</span> instead.
            </p>
          </div>

          <label className="block">
            <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
              Ticker
            </span>
            <input
              ref={symbolRef}
              type="text"
              value={form.symbol}
              onChange={(e) => patch({ symbol: e.target.value.toUpperCase() })}
              disabled={isSaving}
              maxLength={10}
              placeholder="AAPL"
              className={`mt-1 font-mono uppercase ${fieldClass}`}
            />
          </label>

          <div>
            <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
              Action
            </span>
            <div className="mt-1 grid grid-cols-2 gap-2">
              {(['BUY', 'SELL'] as const).map((side) => {
                const active = form.side === side;
                const activeClass =
                  side === 'BUY'
                    ? 'border-win/50 bg-win/15 text-win'
                    : 'border-loss/50 bg-loss/15 text-loss';
                return (
                  <button
                    key={side}
                    type="button"
                    onClick={() => patch({ side })}
                    disabled={isSaving}
                    aria-pressed={active}
                    className={`rounded-lg border px-3 py-2 text-xs font-semibold transition-colors disabled:opacity-50 ${
                      active
                        ? activeClass
                        : 'border-obsidian-border bg-obsidian-bg text-obsidian-muted hover:border-slate-600 hover:text-slate-200'
                    }`}
                  >
                    {side === 'BUY' ? 'Buy' : 'Sell'}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
                Quantity
              </span>
              <input
                type="number"
                inputMode="numeric"
                step="0.00000001"
                min="0"
                value={form.quantity}
                onChange={(e) => patch({ quantity: e.target.value })}
                disabled={isSaving}
                placeholder="100"
                className={`mt-1 font-mono ${fieldClass}`}
              />
            </label>

            <label className="block">
              <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
                Fill Price <span className="text-loss">*</span>
              </span>
              <input
                type="number"
                inputMode="decimal"
                step="0.0001"
                min="0"
                value={form.price}
                onChange={(e) => patch({ price: e.target.value })}
                disabled={isSaving}
                placeholder="150.25"
                className={`mt-1 font-mono ${fieldClass}`}
              />
            </label>
          </div>

          {/* A repair fill stands in for a broker execution that really
              happened, and that execution was charged. Left at zero the
              repaired trade reads as cheaper than every fill beside it, and
              its P&L is the one figure in the journal still computed gross. */}
          <label className="block">
            <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
              Commission
            </span>
            <input
              type="number"
              inputMode="decimal"
              step="0.000001"
              value={form.commission}
              onChange={(e) => patch({ commission: e.target.value })}
              disabled={isSaving}
              placeholder="0.35"
              className={`mt-1 font-mono ${fieldClass}`}
            />
            <span className="mt-1 block text-[10px] text-obsidian-muted">
              What the fill cost to execute, as a positive number. IBKR shows it
              negative on the statement. A rebate goes in negative.
            </span>
          </label>

          <label className="block">
            <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
              Execution Time (ET)
            </span>
            <input
              type="datetime-local"
              value={form.executionTime}
              onChange={(e) => patch({ executionTime: e.target.value })}
              disabled={isSaving}
              className={`mt-1 font-mono ${fieldClass}`}
            />
            <span className="mt-1.5 block text-[10px] text-obsidian-muted">
              US market time — this is what FIFO matching orders fills by, so
              an inaccurate time can pair the wrong entry with the wrong exit.
            </span>
          </label>

          {error && (
            <div className="flex items-start text-xs text-loss">
              <AlertCircle className="mr-1.5 mt-px h-3.5 w-3.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          {savedSummary && (
            <div className="flex items-start text-xs text-win">
              <Check className="mr-1.5 mt-px h-3.5 w-3.5 shrink-0" />
              <span>{savedSummary}</span>
            </div>
          )}

          <div className="flex justify-end gap-2 pt-1">
            <button
              type="button"
              onClick={onClose}
              disabled={isSaving}
              className="rounded-lg border border-obsidian-border bg-obsidian-bg px-3.5 py-2 text-xs font-medium text-obsidian-muted transition-colors hover:border-slate-600 hover:text-slate-200 disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isSaving}
              className="inline-flex items-center gap-2 rounded-lg border border-slate-600 bg-obsidian-bg px-4 py-2 text-xs font-medium text-slate-200 transition-colors hover:bg-white/[0.04] disabled:cursor-not-allowed disabled:opacity-60"
            >
              {isSaving ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  Adding…
                </>
              ) : (
                <>
                  <Wrench className="h-3.5 w-3.5" />
                  Add Fill
                </>
              )}
            </button>
          </div>
        </form>
      </div>
    </div>,
    document.body
  );
}

export default RepairFillModal;

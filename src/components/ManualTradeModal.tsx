'use client';

import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { AlertCircle, Check, Loader2, PlusCircle, X } from 'lucide-react';

import { useCreateManualTrade } from '@/hooks/useTradeInbox';
import type { TradeSide } from '@/types/api';

interface ManualTradeModalProps {
  open: boolean;
  onClose: () => void;
}

interface FormState {
  symbol: string;
  side: TradeSide;
  quantity: string;
  price: string;
  executionTime: string;
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
  executionTime: nowInMarketTz(),
});

export function ManualTradeModal({ open, onClose }: ManualTradeModalProps) {
  const [form, setForm] = useState<FormState>(blankForm);
  const [error, setError] = useState<string | null>(null);
  const [savedSummary, setSavedSummary] = useState<string | null>(null);
  const [mounted, setMounted] = useState(false);
  const symbolRef = useRef<HTMLInputElement>(null);

  const mutation = useCreateManualTrade();

  // The portal target only exists in the browser.
  useEffect(() => setMounted(true), []);

  // Reset to a clean form (with a fresh timestamp) each time it opens.
  useEffect(() => {
    if (open) {
      setForm(blankForm());
      setError(null);
      setSavedSummary(null);
      // Focus the first field so the form is keyboard-ready.
      window.setTimeout(() => symbolRef.current?.focus(), 0);
    }
  }, [open]);

  // Escape closes, matching standard dialog behaviour.
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
    const quantity = Number(form.quantity);
    const price = Number(form.price);

    if (!symbol) return setError('Ticker is required.');
    if (symbol.length > 10) return setError('Ticker must be 10 characters or fewer.');
    if (!Number.isFinite(quantity) || quantity <= 0)
      return setError('Quantity must be a positive number.');
    // Mirrors the API rule: trades.quantity is an INTEGER column.
    if (!Number.isInteger(quantity))
      return setError('Quantity must be a whole number of shares.');
    if (!Number.isFinite(price) || price <= 0)
      return setError('Execution price must be greater than zero.');

    mutation.mutate(
      {
        symbol,
        side: form.side,
        quantity,
        price,
        // Sent without an offset; the backend anchors it to America/New_York.
        execution_time: form.executionTime ? `${form.executionTime}:00` : null,
      },
      {
        onSuccess: (result) => {
          setSavedSummary(
            result.positions_created > 0
              ? `Logged. ${result.positions_created} position${
                  result.positions_created === 1 ? '' : 's'
                } closed by FIFO matching.`
              : `Logged. ${result.open_quantity} share${
                  result.open_quantity === 1 ? '' : 's'
                } open on ${result.ticker}.`
          );
          // Keep the modal open briefly so the outcome is readable.
          window.setTimeout(onClose, 1400);
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

  // Rendered through a portal on <body>. The header that mounts this button
  // uses backdrop-blur, which creates a containing block for fixed-position
  // descendants -- inside it, `fixed inset-0` would resolve against the 64px
  // header instead of the viewport and clip the dialog off-screen.
  return createPortal(
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Log a manual trade"
    >
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/70 backdrop-blur-sm"
        onClick={() => !isSaving && onClose()}
      />

      <div className="relative w-full max-w-md rounded-xl border border-obsidian-border bg-obsidian-card shadow-2xl">
        <div className="flex items-center justify-between px-5 py-4 border-b border-obsidian-border">
          <div className="flex items-center gap-2">
            <PlusCircle className="h-4 w-4 text-win" />
            <h2 className="text-sm font-semibold tracking-wide text-slate-100">
              LOG MANUAL TRADE
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={isSaving}
            aria-label="Close"
            className="p-1 rounded-lg text-obsidian-muted hover:text-slate-200 hover:bg-obsidian-bg transition-colors disabled:opacity-50"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="px-5 py-5 space-y-4">
          <label className="block">
            <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
              Ticker
            </span>
            <input
              ref={symbolRef}
              type="text"
              value={form.symbol}
              // Uppercase as the user types, so what they see is what is sent.
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
                        : 'border-obsidian-border bg-obsidian-bg text-obsidian-muted hover:text-slate-200 hover:border-slate-600'
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
                step="1"
                min="1"
                value={form.quantity}
                onChange={(e) => patch({ quantity: e.target.value })}
                disabled={isSaving}
                placeholder="100"
                className={`mt-1 font-mono ${fieldClass}`}
              />
            </label>

            <label className="block">
              <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
                Price
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
            <span className="text-[10px] text-obsidian-muted">
              US market time — matches how sessions are bucketed.
            </span>
          </label>

          {error && (
            <div className="flex items-start text-xs text-loss">
              <AlertCircle className="h-3.5 w-3.5 mr-1.5 mt-px shrink-0" />
              <span>{error}</span>
            </div>
          )}

          {savedSummary && (
            <div className="flex items-start text-xs text-win">
              <Check className="h-3.5 w-3.5 mr-1.5 mt-px shrink-0" />
              <span>{savedSummary}</span>
            </div>
          )}

          <div className="flex justify-end gap-2 pt-1">
            <button
              type="button"
              onClick={onClose}
              disabled={isSaving}
              className="rounded-lg border border-obsidian-border bg-obsidian-bg px-3.5 py-2 text-xs font-medium text-obsidian-muted hover:text-slate-200 hover:border-slate-600 transition-colors disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isSaving}
              className="inline-flex items-center gap-2 rounded-lg border border-win-border bg-win-glow px-4 py-2 text-xs font-medium text-win hover:bg-win/20 disabled:opacity-60 disabled:cursor-not-allowed transition-colors"
            >
              {isSaving ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  Logging…
                </>
              ) : (
                <>
                  <Check className="h-3.5 w-3.5" />
                  Log Trade
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

export default ManualTradeModal;

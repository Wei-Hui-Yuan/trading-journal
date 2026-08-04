'use client';

import React, { useState } from 'react';
import { AlertCircle, Loader2, X } from 'lucide-react';

import { useCreateInvestmentTransaction } from '@/hooks/useInvestments';
import type { TransactionType } from '@/types/investments';

const TYPES: { value: TransactionType; label: string; blurb: string }[] = [
  { value: 'BUY', label: 'Buy', blurb: 'Shares in, money out' },
  { value: 'SELL', label: 'Sell', blurb: 'Shares out, money in' },
  { value: 'DIVIDEND', label: 'Dividend', blurb: 'Money in, no shares' },
  { value: 'TRANSFER', label: 'Transfer in', blurb: 'Shares in, basis intact' },
];

const INPUT =
  'w-full rounded-lg border border-obsidian-border bg-obsidian-bg px-3 py-2 text-xs ' +
  'text-slate-100 placeholder:text-slate-700 focus:border-slate-600 focus:outline-none';
const LABEL = 'text-[10px] uppercase tracking-wide text-obsidian-muted';

/**
 * Recording what happened. The holding is created alongside the first
 * transaction for a ticker, so a new position is one action rather than two
 * in a fixed order.
 */
export const AddInvestmentModal: React.FC<{
  open: boolean;
  onClose: () => void;
}> = ({ open, onClose }) => {
  const create = useCreateInvestmentTransaction();
  const [error, setError] = useState<string | null>(null);

  const [kind, setKind] = useState<TransactionType>('BUY');
  const [ticker, setTicker] = useState('');
  const [quantity, setQuantity] = useState('');
  const [price, setPrice] = useState('');
  const [amount, setAmount] = useState('');
  const [fees, setFees] = useState('');
  const [date, setDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [note, setNote] = useState('');

  if (!open) return null;

  const isDividend = kind === 'DIVIDEND';

  const reset = () => {
    setTicker('');
    setQuantity('');
    setPrice('');
    setAmount('');
    setFees('');
    setNote('');
    setError(null);
  };

  const submit = () => {
    setError(null);
    if (!ticker.trim()) {
      setError('A ticker is required.');
      return;
    }
    if (isDividend && !amount.trim()) {
      setError('A dividend needs the amount received.');
      return;
    }
    if (!isDividend && (!quantity.trim() || !price.trim())) {
      setError(`A ${kind.toLowerCase()} needs a quantity and a price.`);
      return;
    }

    create.mutate(
      {
        ticker: ticker.trim().toUpperCase(),
        transaction_type: kind,
        quantity: isDividend ? null : Number(quantity),
        price: isDividend ? null : Number(price),
        total_amount: isDividend ? Number(amount) : null,
        fees: fees.trim() ? Number(fees) : 0,
        // Recorded at midday UTC rather than midnight: a date-only entry
        // landing at 00:00 can fall on the previous day once rendered in
        // market time, which silently reorders the ledger.
        transaction_date: new Date(`${date}T12:00:00Z`).toISOString(),
        note: note.trim() || null,
      },
      {
        onSuccess: () => {
          reset();
          onClose();
        },
        onError: (e) => setError(e.message),
      }
    );
  };

  return (
    <div
      className="fixed inset-0 z-[60] flex items-start justify-center overflow-y-auto bg-black/70 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="my-8 w-full max-w-lg rounded-xl border border-obsidian-border bg-obsidian-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between border-b border-obsidian-border px-5 py-4">
          <div>
            <h2 className="text-base font-bold tracking-tight text-slate-100">
              Record a transaction
            </h2>
            <p className="mt-0.5 text-[11px] text-obsidian-muted">
              Quantity and average cost are derived from these, never typed in
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded p-1 text-obsidian-muted transition-colors hover:text-slate-200"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="space-y-4 px-5 py-4">
          <div className="grid grid-cols-4 gap-2">
            {TYPES.map((option) => (
              <button
                key={option.value}
                type="button"
                onClick={() => setKind(option.value)}
                title={option.blurb}
                className={`rounded-lg border px-2 py-2 text-[11px] transition-colors ${
                  kind === option.value
                    ? 'border-emerald-500/50 bg-emerald-500/10 text-emerald-300'
                    : 'border-obsidian-border text-obsidian-muted hover:text-slate-200'
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>

          <label className="block">
            <span className={LABEL}>Ticker</span>
            <input
              value={ticker}
              onChange={(e) => setTicker(e.target.value.toUpperCase())}
              placeholder="GOOGL"
              className={`mt-1 ${INPUT} font-mono`}
            />
            <span className="mt-1 block text-[10px] text-slate-600">
              Share class matters for the data feed — GOOGL resolves where GOOG does not.
            </span>
          </label>

          {isDividend ? (
            <label className="block">
              <span className={LABEL}>Amount received</span>
              <input
                type="number"
                step="any"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                placeholder="38.50"
                className={`mt-1 ${INPUT} font-mono`}
              />
            </label>
          ) : (
            <div className="grid grid-cols-2 gap-3">
              <label className="block">
                <span className={LABEL}>Quantity</span>
                <input
                  type="number"
                  step="any"
                  value={quantity}
                  onChange={(e) => setQuantity(e.target.value)}
                  placeholder="10"
                  className={`mt-1 ${INPUT} font-mono`}
                />
              </label>
              <label className="block">
                <span className={LABEL}>Price per share</span>
                <input
                  type="number"
                  step="any"
                  value={price}
                  onChange={(e) => setPrice(e.target.value)}
                  placeholder="284.16"
                  className={`mt-1 ${INPUT} font-mono`}
                />
              </label>
            </div>
          )}

          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className={LABEL}>Fees</span>
              <input
                type="number"
                step="any"
                value={fees}
                onChange={(e) => setFees(e.target.value)}
                placeholder="0.00"
                className={`mt-1 ${INPUT} font-mono`}
              />
            </label>
            <label className="block">
              <span className={LABEL}>Date</span>
              <input
                type="date"
                value={date}
                onChange={(e) => setDate(e.target.value)}
                className={`mt-1 ${INPUT} font-mono`}
              />
            </label>
          </div>

          <label className="block">
            <span className={LABEL}>Note</span>
            <input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Monthly contribution"
              className={`mt-1 ${INPUT}`}
            />
          </label>

          {error && (
            <div className="flex items-start gap-1.5 rounded border border-loss/30 bg-loss/5 px-3 py-2 text-[11px] text-loss">
              <AlertCircle className="mt-px h-3.5 w-3.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 border-t border-obsidian-border px-5 py-3">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-obsidian-border px-3 py-1.5 text-[11px] text-obsidian-muted transition-colors hover:text-slate-200"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={create.isPending}
            onClick={submit}
            className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-4 py-1.5 text-[11px] font-medium text-emerald-300 transition-colors hover:bg-emerald-500/20 disabled:opacity-50"
          >
            {create.isPending && <Loader2 className="h-3 w-3 animate-spin" />}
            Record
          </button>
        </div>
      </div>
    </div>
  );
};

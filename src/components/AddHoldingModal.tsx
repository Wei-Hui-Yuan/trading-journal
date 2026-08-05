'use client';

import React, { useState } from 'react';
import { AlertCircle, Loader2, X } from 'lucide-react';

import { useCreateHolding } from '@/hooks/useInvestments';
import type { HoldingCategory } from '@/types/investments';

const CATEGORIES: { value: HoldingCategory | ''; label: string }[] = [
  { value: '', label: '— None —' },
  { value: 'Growth', label: 'Growth' },
  { value: 'Predictable', label: 'Predictable' },
  { value: 'ETF', label: 'ETF' },
];

const INPUT =
  'w-full rounded-lg border border-obsidian-border bg-obsidian-bg px-3 py-2 text-xs ' +
  'text-slate-100 placeholder:text-slate-700 focus:border-slate-600 focus:outline-none';
const LABEL = 'text-[10px] uppercase tracking-wide text-obsidian-muted';

/**
 * Adding a ticker to the book with no transaction behind it yet.
 *
 * Every other path into `investment_holdings` goes through a transaction --
 * buying something creates its holding row alongside the fill. This is the
 * other case: you want a ticker classified, priced and valued before you own
 * a share of it, e.g. a watchlist entry, or a position you are about to build
 * up over several purchases and want to see valued from the first one.
 *
 * POST /api/investments/holdings, which already existed with a working hook
 * (useCreateHolding) but no UI ever called it.
 */
export const AddHoldingModal: React.FC<{
  open: boolean;
  onClose: () => void;
}> = ({ open, onClose }) => {
  const create = useCreateHolding();
  const [error, setError] = useState<string | null>(null);

  const [ticker, setTicker] = useState('');
  const [name, setName] = useState('');
  const [sector, setSector] = useState('');
  const [category, setCategory] = useState<HoldingCategory | ''>('');
  const [holdingType, setHoldingType] = useState('');
  const [country, setCountry] = useState('');
  const [currency, setCurrency] = useState('USD');
  const [exchangeRate, setExchangeRate] = useState('1');
  const [allocation, setAllocation] = useState('');
  const [isValuable, setIsValuable] = useState(true);

  if (!open) return null;

  const reset = () => {
    setTicker('');
    setName('');
    setSector('');
    setCategory('');
    setHoldingType('');
    setCountry('');
    setCurrency('USD');
    setExchangeRate('1');
    setAllocation('');
    setIsValuable(true);
    setError(null);
  };

  const submit = () => {
    setError(null);
    if (!ticker.trim()) {
      setError('A ticker is required.');
      return;
    }

    create.mutate(
      {
        ticker: ticker.trim().toUpperCase(),
        name: name.trim() || null,
        sector: sector.trim() || null,
        category: category || null,
        holding_type: holdingType.trim() || null,
        country: country.trim() || null,
        listed_currency: currency.trim().toUpperCase() || 'USD',
        exchange_rate: Number(exchangeRate) || 1,
        planned_allocation: allocation.trim() ? Number(allocation) : null,
        is_valuable: isValuable,
      },
      {
        onSuccess: () => {
          reset();
          onClose();
        },
        // 409 when the ticker already exists -- the backend names it, so the
        // message reads "GOOGL is already in the book" rather than a generic
        // failure with no next step.
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
              Add a stock
            </h2>
            <p className="mt-0.5 text-[11px] text-obsidian-muted">
              Classify a ticker before owning any of it — a watchlist entry, or
              one you are about to build up over several purchases
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
          <label className="block">
            <span className={LABEL}>Ticker</span>
            <input
              value={ticker}
              onChange={(e) => setTicker(e.target.value.toUpperCase())}
              placeholder="MU"
              className={`mt-1 ${INPUT} font-mono`}
              autoFocus
            />
            <span className="mt-1 block text-[10px] text-slate-600">
              Share class matters for the data feed — GOOGL resolves where GOOG does not.
            </span>
          </label>

          <label className="block">
            <span className={LABEL}>Name</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Micron Technology"
              className={`mt-1 ${INPUT}`}
            />
          </label>

          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className={LABEL}>Sector</span>
              <input
                value={sector}
                onChange={(e) => setSector(e.target.value)}
                placeholder="Technology"
                className={`mt-1 ${INPUT}`}
              />
            </label>
            <label className="block">
              <span className={LABEL}>Category</span>
              <select
                value={category}
                onChange={(e) => setCategory(e.target.value as HoldingCategory | '')}
                className={`mt-1 ${INPUT}`}
              >
                {CATEGORIES.map((c) => (
                  <option key={c.value} value={c.value}>
                    {c.label}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className={LABEL}>Type</span>
              <input
                value={holdingType}
                onChange={(e) => setHoldingType(e.target.value)}
                placeholder="Moderate Cyclical"
                className={`mt-1 ${INPUT}`}
              />
            </label>
            <label className="block">
              <span className={LABEL}>Country</span>
              <input
                value={country}
                onChange={(e) => setCountry(e.target.value)}
                placeholder="US"
                className={`mt-1 ${INPUT}`}
              />
            </label>
          </div>

          <div className="grid grid-cols-3 gap-3">
            <label className="block">
              <span className={LABEL}>Currency</span>
              <input
                value={currency}
                onChange={(e) => setCurrency(e.target.value.toUpperCase())}
                maxLength={3}
                className={`mt-1 ${INPUT} font-mono`}
              />
            </label>
            <label className="block">
              <span className={LABEL}>Exch. rate</span>
              <input
                type="number"
                step="any"
                value={exchangeRate}
                onChange={(e) => setExchangeRate(e.target.value)}
                className={`mt-1 ${INPUT} font-mono`}
              />
              <span className="mt-1 block text-[10px] text-slate-600">1 USD in this currency</span>
            </label>
            <label className="block">
              <span className={LABEL}>Allocation</span>
              <input
                type="number"
                step="any"
                value={allocation}
                onChange={(e) => setAllocation(e.target.value)}
                placeholder="1000"
                className={`mt-1 ${INPUT} font-mono`}
              />
            </label>
          </div>

          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              checked={isValuable}
              onChange={(e) => setIsValuable(e.target.checked)}
              className="mt-0.5 h-3.5 w-3.5 rounded border-obsidian-border bg-obsidian-bg accent-emerald-500"
            />
            <span className="text-[11px] text-slate-300">
              Run the DCF on this holding
              <span className="mt-0.5 block text-[10px] text-obsidian-muted">
                Uncheck for a fund with no cash flows of its own, e.g. an ETF —
                it will be skipped by every valuation refresh.
              </span>
            </span>
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
            Add stock
          </button>
        </div>
      </div>
    </div>
  );
};

'use client';

import React, { useState } from 'react';
import { AlertCircle, Loader2, Trash2, X } from 'lucide-react';

import { useDeleteHolding, useUpdateHolding } from '@/hooks/useInvestments';
import type { Holding, HoldingCategory } from '@/types/investments';
import { ConfirmDialog } from './ConfirmDialog';

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
 * Reclassifying a holding, or removing one that never had a share of it.
 *
 * The ticker is fixed -- it is the primary key and the URL path PATCH goes
 * to, so editing it in place would rewrite what row this even is. Every
 * other HoldingPayload field is here, PATCH and DELETE both already existed
 * on the backend with working hooks and no UI ever called them.
 */
export const EditHoldingModal: React.FC<{
  holding: Holding;
  onClose: () => void;
}> = ({ holding, onClose }) => {
  const update = useUpdateHolding();
  const del = useDeleteHolding();
  const [error, setError] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const [name, setName] = useState(holding.name ?? '');
  const [sector, setSector] = useState(holding.sector ?? '');
  const [category, setCategory] = useState<HoldingCategory | ''>(holding.category ?? '');
  const [holdingType, setHoldingType] = useState(holding.holding_type ?? '');
  const [country, setCountry] = useState(holding.country ?? '');
  const [currency, setCurrency] = useState(holding.listed_currency);
  const [exchangeRate, setExchangeRate] = useState(String(holding.exchange_rate));
  const [allocation, setAllocation] = useState(
    holding.planned_allocation === null ? '' : String(holding.planned_allocation)
  );
  const [isValuable, setIsValuable] = useState(holding.is_valuable);

  const save = () => {
    setError(null);
    update.mutate(
      {
        ticker: holding.ticker,
        payload: {
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
      },
      { onSuccess: onClose, onError: (e) => setError(e.message) }
    );
  };

  return (
    <>
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
                Edit {holding.ticker}
              </h2>
              <p className="mt-0.5 text-[11px] text-obsidian-muted">
                {holding.transaction_count > 0
                  ? `${holding.transaction_count} transaction(s) on record — classification only, quantity is derived`
                  : 'No transactions yet — this holding can be deleted below'}
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
              <span className={LABEL}>Name</span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                className={`mt-1 ${INPUT}`}
              />
            </label>

            <div className="grid grid-cols-2 gap-3">
              <label className="block">
                <span className={LABEL}>Sector</span>
                <input
                  value={sector}
                  onChange={(e) => setSector(e.target.value)}
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
                  className={`mt-1 ${INPUT}`}
                />
              </label>
              <label className="block">
                <span className={LABEL}>Country</span>
                <input
                  value={country}
                  onChange={(e) => setCountry(e.target.value)}
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
              </label>
              <label className="block">
                <span className={LABEL}>Allocation</span>
                <input
                  type="number"
                  step="any"
                  value={allocation}
                  onChange={(e) => setAllocation(e.target.value)}
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
                  Unchecking skips it in every valuation refresh, e.g. for an ETF.
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

          <div className="flex items-center justify-between gap-2 border-t border-obsidian-border px-5 py-3">
            <button
              type="button"
              onClick={() => {
                setError(null);
                setConfirmingDelete(true);
              }}
              className="inline-flex items-center gap-1.5 rounded-lg border border-loss/20 bg-loss/10 px-3 py-1.5 text-[11px] text-loss transition-colors hover:bg-loss/20"
            >
              <Trash2 className="h-3 w-3" />
              Delete holding
            </button>

            <div className="flex gap-2">
              <button
                type="button"
                onClick={onClose}
                className="rounded-lg border border-obsidian-border px-3 py-1.5 text-[11px] text-obsidian-muted transition-colors hover:text-slate-200"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={update.isPending}
                onClick={save}
                className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-4 py-1.5 text-[11px] font-medium text-emerald-300 transition-colors hover:bg-emerald-500/20 disabled:opacity-50"
              >
                {update.isPending && <Loader2 className="h-3 w-3 animate-spin" />}
                Save
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* A fragment-level sibling of the backdrop above, not a descendant of
          it -- ConfirmDialog portals its DOM to document.body, but React's
          synthetic events bubble through the REACT tree regardless of where
          the DOM lands. Nested inside the backdrop div, a click on its own
          buttons would bubble to that div's onClick={onClose} and close this
          whole modal as a side effect of confirming or cancelling the
          delete -- exactly the bug this shape was built to avoid, caught
          live in TransactionLedgerModal before this component existed. */}
      <ConfirmDialog
        open={confirmingDelete}
        title={`Delete ${holding.ticker}?`}
        confirmLabel={del.isPending ? 'Deleting…' : 'Delete'}
        confirmDisabled={del.isPending}
        onCancel={() => setConfirmingDelete(false)}
        onConfirm={() => {
          setError(null);
          del.mutate(holding.ticker, {
            onSuccess: () => {
              setConfirmingDelete(false);
              onClose();
            },
            onError: (e) => {
              // Left open rather than closed on failure -- the refusal (a
              // transaction still exists) names what to do next, and closing
              // the modal would bury that where it cannot be acted on.
              setConfirmingDelete(false);
              setError(e.message);
            },
          });
        }}
      >
        <p>
          Removes {holding.ticker} from the book entirely, including its saved valuation
          inputs. Refused if any transactions are still on record — this only works for a
          holding with none.
        </p>
      </ConfirmDialog>
    </>
  );
};

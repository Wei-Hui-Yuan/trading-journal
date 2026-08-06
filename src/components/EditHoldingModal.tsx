'use client';

import React, { useState } from 'react';
import { AlertCircle, Loader2, Trash2, X } from 'lucide-react';

import { useCorrectBasis, useDeleteHolding, useUpdateHolding } from '@/hooks/useInvestments';
import type { Holding, HoldingCategory } from '@/types/investments';
import { ConfirmDialog } from './ConfirmDialog';
import { Combobox } from './Combobox';

/** Mirrors the CHECK in migration 026 -- a closed set, so the combobox below
 * is given `allowCustom={false}` rather than letting the trader type a
 * category the backend will refuse to save. */
const CATEGORY_VALUES: HoldingCategory[] = ['Growth', 'Predictable', 'ETF'];

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
  /** Distinct values already used elsewhere in the book, for each combobox. */
  sectorOptions: string[];
  typeOptions: string[];
  countryOptions: string[];
  currencyOptions: string[];
  /** Sum of cost basis across every holding, for the allocation weight
   * preview below. */
  totalCostBasis: number;
}> = ({
  holding,
  onClose,
  sectorOptions,
  typeOptions,
  countryOptions,
  currencyOptions,
  totalCostBasis,
}) => {
  const update = useUpdateHolding();
  const correctBasis = useCorrectBasis();
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

  // Quantity and average cost default to what the ledger derives, editable
  // when it is wrong -- saving a changed value writes a correction to the
  // ledger rather than a display override, see useCorrectBasis. Only offered
  // once a position actually exists: with no transactions there is nothing
  // for a correction to be relative to.
  const hasPosition = holding.transaction_count > 0;
  const [quantity, setQuantity] = useState(String(holding.quantity));
  // Rounded to cents for the starting value -- the derived figure carries
  // binary-float noise (e.g. 430.7776444444444) that is real but not
  // meaningful to type back, unlike quantity, where a fractional share is
  // itself the actual position and stays unrounded.
  const [averageCost, setAverageCost] = useState(
    holding.average_cost !== null ? holding.average_cost.toFixed(2) : ''
  );
  // Price is a plain override column (migration 028) rather than a ledger
  // correction -- refresh_prices keeps overwriting the auto figure
  // underneath it, so blank here just means "use the live quote".
  const [manualPrice, setManualPrice] = useState(
    holding.manual_price !== null ? String(holding.manual_price) : ''
  );

  const quantityNum = Number(quantity);
  const averageCostNum = Number(averageCost);
  const quantityChanged =
    hasPosition && Number.isFinite(quantityNum) && Math.abs(quantityNum - holding.quantity) > 1e-9;
  // Half a cent, not 1e-6 -- the starting value above is ROUNDED to cents,
  // so comparing against the unrounded derived figure at float precision
  // would call an untouched field "changed" and fire a no-op correction.
  const averageCostChanged =
    hasPosition &&
    Number.isFinite(averageCostNum) &&
    Math.abs(averageCostNum - (holding.average_cost ?? 0)) > 0.005;
  const needsCorrection = quantityChanged || averageCostChanged;

  // Preview only -- what this target WOULD weigh once funded, against the
  // rest of the book. This holding's own current cost basis is subtracted out
  // of the base first so it is not counted twice: once as what it costs
  // today, and again inside the target being previewed.
  const allocationNum = Number(allocation);
  const restOfBook = Math.max(0, totalCostBasis - holding.cost_basis);
  const projectedWeightPct =
    allocation.trim() && Number.isFinite(allocationNum) && allocationNum > 0
      ? (allocationNum / (restOfBook + allocationNum)) * 100
      : null;

  const save = () => {
    setError(null);

    if (quantityChanged && (!Number.isFinite(quantityNum) || quantityNum < 0)) {
      setError('Quantity cannot be negative.');
      return;
    }
    if (averageCostChanged && (!Number.isFinite(averageCostNum) || averageCostNum < 0)) {
      setError('Average cost cannot be negative.');
      return;
    }
    const manualPriceNum = manualPrice.trim() ? Number(manualPrice) : null;
    if (manualPriceNum !== null && (!Number.isFinite(manualPriceNum) || manualPriceNum < 0)) {
      setError('Price cannot be negative.');
      return;
    }

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
          manual_price: manualPriceNum,
        },
      },
      {
        onSuccess: () => {
          if (!needsCorrection) {
            onClose();
            return;
          }
          // A separate call on purpose -- classification is a PATCH to the
          // holding row, a correction is a new row on the ledger. Firing
          // both from one Save keeps the modal feeling like a single edit.
          correctBasis.mutate(
            {
              ticker: holding.ticker,
              payload: {
                quantity: quantityChanged ? quantityNum : undefined,
                average_cost: averageCostChanged ? averageCostNum : undefined,
              },
            },
            { onSuccess: onClose, onError: (e) => setError(e.message) }
          );
        },
        onError: (e) => setError(e.message),
      }
    );
  };

  const saving = update.isPending || correctBasis.isPending;

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
                <Combobox
                  value={sector}
                  onChange={setSector}
                  options={sectorOptions}
                  className={`mt-1 ${INPUT}`}
                />
              </label>
              <label className="block">
                <span className={LABEL}>Category</span>
                <Combobox
                  value={category}
                  onChange={(v) => setCategory(v as HoldingCategory | '')}
                  options={CATEGORY_VALUES}
                  allowCustom={false}
                  placeholder="— None —"
                  className={`mt-1 ${INPUT}`}
                />
              </label>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <label className="block">
                <span className={LABEL}>Type</span>
                <Combobox
                  value={holdingType}
                  onChange={setHoldingType}
                  options={typeOptions}
                  className={`mt-1 ${INPUT}`}
                />
              </label>
              <label className="block">
                <span className={LABEL}>Country</span>
                <Combobox
                  value={country}
                  onChange={setCountry}
                  options={countryOptions}
                  className={`mt-1 ${INPUT}`}
                />
              </label>
            </div>

            <div className="grid grid-cols-3 gap-3">
              <label className="block">
                <span className={LABEL}>Currency</span>
                <Combobox
                  value={currency}
                  onChange={(v) => setCurrency(v.toUpperCase())}
                  options={currencyOptions}
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
                {projectedWeightPct !== null && (
                  <span className="mt-1 block text-[10px] text-slate-600">
                    {projectedWeightPct.toFixed(1)}% of the book once funded
                  </span>
                )}
              </label>
            </div>

            <div className="space-y-1 border-t border-obsidian-border/50 pt-3.5">
              <p className="text-[10px] text-obsidian-muted">
                Position, derived from the ledger — edit only to correct one that&rsquo;s wrong.
                Saving writes a correction to the ledger, not a display override.
              </p>
              {hasPosition ? (
                <div className="grid grid-cols-2 gap-3">
                  <label className="block">
                    <span className={LABEL}>Quantity</span>
                    <input
                      type="number"
                      step="any"
                      value={quantity}
                      onChange={(e) => setQuantity(e.target.value)}
                      className={`mt-1 ${INPUT} font-mono`}
                    />
                    <span className="mt-1 flex items-center gap-1.5 text-[10px] text-slate-600">
                      derived: {holding.quantity}
                      {quantityChanged && (
                        <button
                          type="button"
                          onClick={() => setQuantity(String(holding.quantity))}
                          className="text-slate-500 underline decoration-dotted hover:text-slate-300"
                        >
                          reset
                        </button>
                      )}
                    </span>
                  </label>
                  <label className="block">
                    <span className={LABEL}>Avg cost</span>
                    <input
                      type="number"
                      step="any"
                      value={averageCost}
                      onChange={(e) => setAverageCost(e.target.value)}
                      className={`mt-1 ${INPUT} font-mono`}
                    />
                    <span className="mt-1 flex items-center gap-1.5 text-[10px] text-slate-600">
                      derived: {holding.average_cost !== null ? holding.average_cost.toFixed(2) : '—'}
                      {averageCostChanged && (
                        <button
                          type="button"
                          onClick={() =>
                            setAverageCost(
                              holding.average_cost !== null ? String(holding.average_cost) : ''
                            )
                          }
                          className="text-slate-500 underline decoration-dotted hover:text-slate-300"
                        >
                          reset
                        </button>
                      )}
                    </span>
                  </label>
                </div>
              ) : (
                <p className="text-[10px] text-slate-600">
                  No transactions yet — record one to establish a position before correcting
                  quantity or cost.
                </p>
              )}

              <label className="block">
                <span className={LABEL}>Price</span>
                <input
                  type="number"
                  step="any"
                  value={manualPrice}
                  onChange={(e) => setManualPrice(e.target.value)}
                  placeholder={holding.auto_price !== null ? holding.auto_price.toFixed(2) : 'no quote yet'}
                  className={`mt-1 ${INPUT} font-mono`}
                />
                <span className="mt-1 flex items-center gap-1.5 text-[10px] text-slate-600">
                  {holding.auto_price !== null
                    ? `auto: ${holding.auto_price.toFixed(2)}`
                    : 'no auto quote yet'}
                  {manualPrice.trim() && (
                    <button
                      type="button"
                      onClick={() => setManualPrice('')}
                      className="text-slate-500 underline decoration-dotted hover:text-slate-300"
                    >
                      reset to auto
                    </button>
                  )}
                </span>
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
                disabled={saving}
                onClick={save}
                className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-4 py-1.5 text-[11px] font-medium text-emerald-300 transition-colors hover:bg-emerald-500/20 disabled:opacity-50"
              >
                {saving && <Loader2 className="h-3 w-3 animate-spin" />}
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

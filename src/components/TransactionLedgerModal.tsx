'use client';

import React, { useState } from 'react';
import { AlertCircle, Loader2, Pencil, Trash2, X } from 'lucide-react';

import {
  useDeleteInvestmentTransaction,
  useInvestmentTransactions,
  useUpdateInvestmentTransaction,
} from '@/hooks/useInvestments';
import type { InvestmentTransaction } from '@/types/investments';
import { ConfirmDialog } from './ConfirmDialog';

const dateFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  year: 'numeric',
  month: 'short',
  day: '2-digit',
});

/** Trailing zeros on a fractional share count are noise. */
function quantity(value: number | null): string {
  if (value === null) return '—';
  return Number(value.toFixed(8)).toLocaleString('en-US', { maximumFractionDigits: 8 });
}

function money(value: number | null): string {
  if (value === null) return '—';
  return value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** In-progress correction. Strings, so "empty" and mid-typing states are
 * representable -- a controlled number input cannot hold either otherwise. */
interface Draft {
  quantity: string;
  price: string;
  fees: string;
  date: string; // yyyy-mm-dd
  note: string;
}

const BLANK_DRAFT: Draft = { quantity: '', price: '', fees: '', date: '', note: '' };

function draftFrom(tx: InvestmentTransaction): Draft {
  return {
    quantity: tx.quantity === null ? '' : String(tx.quantity),
    price: tx.price === null ? '' : String(tx.price),
    fees: String(tx.fees),
    date: tx.transaction_date.slice(0, 10),
    note: tx.note ?? '',
  };
}

const INPUT =
  'w-full rounded border border-obsidian-border bg-obsidian-bg px-1.5 py-1 text-[11px] ' +
  'text-slate-100 focus:border-slate-600 focus:outline-none';

/**
 * The raw ledger for one ticker: every BUY, SELL, DIVIDEND and TRANSFER that
 * derives its quantity and average cost.
 *
 * The gap this closes: transactions could be created but never seen again --
 * GET/PATCH/DELETE all existed on the backend with a working hook for create
 * and delete, but nothing in the UI ever listed a ticker's history, so fixing
 * a fat-fingered entry meant asking for a direct database edit.
 */
export const TransactionLedgerModal: React.FC<{
  ticker: string;
  onClose: () => void;
}> = ({ ticker, onClose }) => {
  const { data: transactions, isLoading, error } = useInvestmentTransactions(ticker);
  const update = useUpdateInvestmentTransaction();
  const del = useDeleteInvestmentTransaction();

  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(BLANK_DRAFT);
  const [confirming, setConfirming] = useState<InvestmentTransaction | null>(null);
  const [rowError, setRowError] = useState<string | null>(null);

  const rows = transactions ?? [];

  const startEdit = (tx: InvestmentTransaction) => {
    setEditingId(tx.id);
    setDraft(draftFrom(tx));
    setRowError(null);
  };

  const save = (tx: InvestmentTransaction) => {
    setRowError(null);
    const isDividend = tx.transaction_type === 'DIVIDEND';

    if (!isDividend) {
      const qty = Number(draft.quantity);
      if (!Number.isFinite(qty) || qty <= 0) {
        setRowError('Quantity must be greater than zero.');
        return;
      }
      const price = Number(draft.price);
      if (!Number.isFinite(price) || price < 0) {
        setRowError('Price cannot be negative.');
        return;
      }
    }
    const fees = Number(draft.fees);
    if (!Number.isFinite(fees) || fees < 0) {
      setRowError('Fees cannot be negative.');
      return;
    }

    update.mutate(
      {
        id: tx.id,
        payload: {
          ...(isDividend ? {} : { quantity: Number(draft.quantity), price: Number(draft.price) }),
          fees,
          // Sent as noon UTC, matching how the create form records a
          // date-only entry -- midnight would land on the previous day once
          // rendered in market time and silently reorder the ledger.
          transaction_date: new Date(`${draft.date}T12:00:00Z`).toISOString(),
          note: draft.note.trim() || null,
        },
      },
      {
        onSuccess: () => setEditingId(null),
        onError: (e) => setRowError(e.message),
      }
    );
  };

  return (
    // A fragment, not a single backdrop div, is what matters here: ConfirmDialog
    // portals its DOM to document.body, but React's synthetic events bubble
    // through the REACT tree, not the DOM tree. Nested one level deeper as a
    // child of the backdrop div below, a click on its Delete button would
    // bubble to that div's own onClick={onClose} and close this whole modal
    // as an unintended side effect -- which is exactly what happened before
    // this was a fragment, confirmed live: deleting a row silently closed the
    // ledger instead of just removing the row.
    <>
      <div
        className="fixed inset-0 z-[60] flex items-start justify-center overflow-y-auto bg-black/70 p-4 backdrop-blur-sm"
        onClick={onClose}
      >
      <div
        className="my-8 w-full max-w-3xl rounded-xl border border-obsidian-border bg-obsidian-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between border-b border-obsidian-border px-5 py-4">
          <div>
            <h2 className="text-base font-bold tracking-tight text-slate-100">
              {ticker} <span className="font-normal text-obsidian-muted">transactions</span>
            </h2>
            <p className="mt-0.5 text-[11px] text-obsidian-muted">
              Quantity and average cost on the portfolio table are derived from these rows.
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

        <div className="px-5 py-4">
          {isLoading ? (
            <div className="flex items-center gap-2 py-8 text-sm text-obsidian-muted">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading…
            </div>
          ) : error ? (
            <div className="flex items-start gap-2 rounded border border-loss/30 bg-loss/5 p-3 text-xs text-loss">
              <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{error.message}</span>
            </div>
          ) : rows.length === 0 ? (
            <p className="py-8 text-center text-sm text-obsidian-muted">
              No transactions recorded for {ticker} yet.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[620px] text-left text-[11px]">
                <thead className="text-obsidian-muted">
                  <tr>
                    <th className="pb-1.5 font-normal">Date</th>
                    <th className="pb-1.5 font-normal">Type</th>
                    <th className="pb-1.5 text-right font-normal">Qty</th>
                    <th className="pb-1.5 text-right font-normal">Price</th>
                    <th className="pb-1.5 text-right font-normal">Total</th>
                    <th className="pb-1.5 text-right font-normal">Fees</th>
                    <th className="pb-1.5 font-normal">Note</th>
                    <th className="pb-1.5 text-right font-normal">Action</th>
                  </tr>
                </thead>
                <tbody className="font-mono text-slate-300">
                  {rows.map((tx) =>
                    editingId === tx.id ? (
                      <tr key={tx.id} className="border-t border-obsidian-border/60 bg-obsidian-bg/40">
                        <td className="py-1.5 pr-2">
                          <input
                            type="date"
                            value={draft.date}
                            onChange={(e) => setDraft({ ...draft, date: e.target.value })}
                            className={INPUT}
                          />
                        </td>
                        <td className="py-1.5 pr-2 font-sans text-slate-400">
                          {tx.transaction_type}
                        </td>
                        <td className="py-1.5 pr-2">
                          {tx.transaction_type === 'DIVIDEND' ? (
                            '—'
                          ) : (
                            <input
                              type="number"
                              step="any"
                              value={draft.quantity}
                              onChange={(e) => setDraft({ ...draft, quantity: e.target.value })}
                              className={`${INPUT} w-20 text-right`}
                            />
                          )}
                        </td>
                        <td className="py-1.5 pr-2">
                          {tx.transaction_type === 'DIVIDEND' ? (
                            '—'
                          ) : (
                            <input
                              type="number"
                              step="any"
                              value={draft.price}
                              onChange={(e) => setDraft({ ...draft, price: e.target.value })}
                              className={`${INPUT} w-24 text-right`}
                            />
                          )}
                        </td>
                        {/* total_amount is not directly editable here -- the
                            backend recomputes it from quantity/price/fees
                            whenever those change, the same rule the create
                            form follows. Editing it in place would let the
                            two silently disagree. */}
                        <td className="py-1.5 pr-2 text-right text-slate-500">
                          {money(tx.total_amount)}
                        </td>
                        <td className="py-1.5 pr-2">
                          <input
                            type="number"
                            step="any"
                            value={draft.fees}
                            onChange={(e) => setDraft({ ...draft, fees: e.target.value })}
                            className={`${INPUT} w-16 text-right`}
                          />
                        </td>
                        <td className="py-1.5 pr-2">
                          <input
                            value={draft.note}
                            onChange={(e) => setDraft({ ...draft, note: e.target.value })}
                            className={`${INPUT} font-sans`}
                          />
                        </td>
                        <td className="py-1.5 text-right">
                          <div className="inline-flex gap-1">
                            <button
                              type="button"
                              onClick={() => save(tx)}
                              disabled={update.isPending}
                              className="rounded border border-win-border bg-win-glow px-2 py-0.5 font-sans text-[10px] text-win transition-colors hover:bg-win/20 disabled:opacity-50"
                            >
                              {update.isPending ? 'Saving…' : 'Save'}
                            </button>
                            <button
                              type="button"
                              onClick={() => setEditingId(null)}
                              disabled={update.isPending}
                              aria-label="Cancel edit"
                              className="rounded border border-obsidian-border px-1.5 py-0.5 text-obsidian-muted transition-colors hover:text-slate-200 disabled:opacity-50"
                            >
                              <X className="h-3 w-3" />
                            </button>
                          </div>
                        </td>
                      </tr>
                    ) : (
                      <tr key={tx.id} className="border-t border-obsidian-border/60">
                        <td className="py-1.5 text-slate-400">
                          {dateFmt.format(new Date(tx.transaction_date))}
                        </td>
                        <td className="py-1.5 font-sans">
                          <span
                            className={
                              tx.transaction_type === 'SELL'
                                ? 'text-loss'
                                : tx.transaction_type === 'DIVIDEND'
                                  ? 'text-win'
                                  : tx.transaction_type === 'ADJUSTMENT'
                                    ? 'text-sky-400'
                                    : 'text-slate-300'
                            }
                          >
                            {tx.transaction_type}
                          </span>
                          {tx.source !== 'MANUAL' && (
                            <span className="ml-1 text-[9px] text-obsidian-muted">
                              {tx.source}
                            </span>
                          )}
                        </td>
                        <td className="py-1.5 text-right">{quantity(tx.quantity)}</td>
                        <td className="py-1.5 text-right">{money(tx.price)}</td>
                        <td className="py-1.5 text-right text-slate-100">
                          {money(tx.total_amount)}
                        </td>
                        <td className="py-1.5 text-right text-obsidian-muted">
                          {money(tx.fees)}
                        </td>
                        <td className="max-w-[140px] truncate py-1.5 font-sans text-obsidian-muted">
                          {tx.note ?? ''}
                        </td>
                        <td className="py-1.5 text-right">
                          <div className="inline-flex gap-1 font-sans">
                            {/* No pencil for a correction row -- its quantity
                                and total_amount are signed deltas, which this
                                form's "quantity must be > 0" rule (below)
                                would reject even when re-saved unchanged.
                                Wrong correction: delete it and correct again
                                from the edit modal. */}
                            {tx.transaction_type !== 'ADJUSTMENT' && (
                              <button
                                type="button"
                                onClick={() => startEdit(tx)}
                                title="Correct this transaction"
                                className="inline-flex items-center gap-1 rounded border border-obsidian-border px-2 py-0.5 text-[10px] text-obsidian-muted transition-colors hover:border-slate-600 hover:text-slate-200"
                              >
                                <Pencil className="h-3 w-3" />
                              </button>
                            )}
                            <button
                              type="button"
                              onClick={() => setConfirming(tx)}
                              title="Delete this transaction"
                              className="inline-flex items-center gap-1 rounded border border-loss/20 bg-loss/10 px-2 py-0.5 text-[10px] text-loss transition-colors hover:bg-loss/20"
                            >
                              <Trash2 className="h-3 w-3" />
                            </button>
                          </div>
                        </td>
                      </tr>
                    )
                  )}
                </tbody>
              </table>
            </div>
          )}

          {rowError && (
            <div className="mt-3 flex items-start gap-1.5 rounded border border-loss/30 bg-loss/5 px-3 py-2 text-[11px] text-loss">
              <AlertCircle className="mt-px h-3.5 w-3.5 shrink-0" />
              <span>{rowError}</span>
            </div>
          )}
        </div>
      </div>
      </div>

      <ConfirmDialog
        open={confirming !== null}
        title="Delete this transaction?"
        confirmLabel={del.isPending ? 'Deleting…' : 'Delete'}
        confirmDisabled={del.isPending}
        onCancel={() => setConfirming(null)}
        onConfirm={() => {
          if (!confirming) return;
          del.mutate(confirming.id, {
            onSuccess: () => setConfirming(null),
            onError: (e) => {
              setRowError(e.message);
              setConfirming(null);
            },
          });
        }}
      >
        {confirming && (
          <p>
            {confirming.transaction_type === 'DIVIDEND' ? (
              <>
                Removes the {money(confirming.total_amount)} dividend recorded{' '}
                {dateFmt.format(new Date(confirming.transaction_date))}.
              </>
            ) : confirming.transaction_type === 'ADJUSTMENT' ? (
              <>
                Removes the correction of {quantity(confirming.quantity)} shares /{' '}
                {money(confirming.total_amount)} cost, recorded{' '}
                {dateFmt.format(new Date(confirming.transaction_date))}.
              </>
            ) : (
              <>
                Removes the {confirming.transaction_type.toLowerCase()} of{' '}
                {quantity(confirming.quantity)} shares at {money(confirming.price)}, recorded{' '}
                {dateFmt.format(new Date(confirming.transaction_date))}.
              </>
            )}{' '}
            {ticker}&rsquo;s quantity and average cost will be recalculated from what remains.
            This cannot be undone.
          </p>
        )}
      </ConfirmDialog>
    </>
  );
};

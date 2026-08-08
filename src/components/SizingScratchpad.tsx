'use client';

import React, { useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import {
  AlertCircle,
  ArrowUpRight,
  Calculator,
  Check,
  Loader2,
  Trash2,
} from 'lucide-react';

import {
  useCreateSizingEntry,
  useDeleteSizingEntry,
  usePromoteSizingEntry,
  useSizingScratchpad,
  useUpdateSizingEntry,
} from '@/hooks/useSizingScratchpad';
import { useSettings } from '@/hooks/useTradeInbox';
import {
  computeSizing,
  scoreTakeProfit,
  sizingHint,
  type Side,
} from '@/lib/positionSizing';
import type { SizingScratchpadEntry } from '@/types/sizing';

/** Money, to the cent — unsigned, matching the Plan modal's own local
 * formatter rather than the app-wide signed one, which is for P&L. */
const money = (n: number) =>
  n.toLocaleString('en-US', { style: 'currency', currency: 'USD' });

/** A price, at the precision the instrument warrants — sub-dollar tickers
 * need more than two decimals or every target rounds to the same number. */
const price = (n: number) => (n < 1 ? n.toFixed(4) : n.toFixed(2));

function toNullableNumber(raw: string): number | null {
  const trimmed = raw.trim();
  if (trimmed === '') return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
}

/**
 * How long ago, in the compact form a note that lives at most three days
 * deserves. Never a calendar date — nothing here is ever old enough for one
 * to be the more useful answer.
 */
function relativeAge(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

const inputClass =
  'w-full rounded-lg bg-obsidian-bg border border-obsidian-border px-2.5 py-1.5 text-xs text-slate-200 ' +
  'font-mono placeholder:text-obsidian-muted placeholder:font-sans focus:outline-none focus:border-slate-600 ' +
  'transition-colors disabled:opacity-50';

/**
 * A number field that commits on blur, not on every keystroke.
 *
 * PATCHing per keystroke would fire a request for every digit of a five-digit
 * price and race itself on the network; blur is the natural "I am done with
 * this field" signal a form already gets for free. Resyncs from `value`
 * after a commit — including one made from a DIFFERENT field in the same row
 * invalidating the query — so the display never drifts from what the server
 * actually has.
 */
const EditableNumber: React.FC<{
  value: number | null;
  onCommit: (next: number | null) => void;
  placeholder?: string;
  disabled?: boolean;
}> = ({ value, onCommit, placeholder, disabled }) => {
  const [text, setText] = useState(value === null ? '' : String(value));

  useEffect(() => {
    setText(value === null ? '' : String(value));
  }, [value]);

  return (
    <input
      type="number"
      inputMode="decimal"
      step="any"
      value={text}
      placeholder={placeholder}
      disabled={disabled}
      onChange={(e) => setText(e.target.value)}
      onBlur={() => {
        const next = toNullableNumber(text);
        if (next !== value) onCommit(next);
      }}
      className={inputClass}
    />
  );
};

/** What one saved note computes to, using the exact same library the Plan
 * modal does — the scratchpad and a real plan must never disagree about
 * what a given entry/stop/quantity means. */
function useRowSizing(entry: SizingScratchpadEntry) {
  const side = entry.direction as Side;
  const sizing = useMemo(
    () =>
      computeSizing({
        side,
        entry: entry.entry,
        stop: entry.stop_loss,
        accountSize: null,
        riskPercent: null,
      }),
    [side, entry.entry, entry.stop_loss]
  );

  const riskAmount =
    sizing !== null && entry.quantity !== null
      ? sizing.riskPerShare * entry.quantity
      : null;

  const tpScore = useMemo(() => {
    if (entry.entry === null || entry.take_profit === null || sizing === null) {
      return null;
    }
    return scoreTakeProfit({
      side,
      entry: entry.entry,
      takeProfit: entry.take_profit,
      riskPerShare: sizing.riskPerShare,
      shares: entry.quantity,
    });
  }, [side, entry.entry, entry.take_profit, entry.quantity, sizing]);

  return { sizing, riskAmount, tpScore };
}

const ScratchpadRow: React.FC<{ entry: SizingScratchpadEntry }> = ({ entry }) => {
  const updateMutation = useUpdateSizingEntry();
  const deleteMutation = useDeleteSizingEntry();
  const promoteMutation = usePromoteSizingEntry();
  const [promoteError, setPromoteError] = useState<string | null>(null);

  const { sizing, riskAmount, tpScore } = useRowSizing(entry);

  const patch = (
    fields: Partial<{
      ticker: string;
      direction: Side;
      entry: number | null;
      stop_loss: number | null;
      take_profit: number | null;
      quantity: number | null;
    }>
  ) => {
    updateMutation.mutate({ id: entry.id, payload: fields });
  };

  return (
    <div className="rounded-lg border border-obsidian-border bg-obsidian-bg/40 p-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-6">
        <input
          type="text"
          value={entry.ticker}
          onChange={(e) =>
            patch({ ticker: e.target.value.trim().toUpperCase() })
          }
          maxLength={10}
          className={`${inputClass} font-sans font-semibold uppercase`}
        />

        <div className="grid grid-cols-2 gap-1">
          {(['BUY', 'SELL'] as const).map((side) => {
            const active = entry.direction === side;
            return (
              <button
                key={side}
                type="button"
                onClick={() => patch({ direction: side })}
                aria-pressed={active}
                className={`rounded-lg border px-1.5 py-1.5 text-[10px] font-semibold transition-colors ${
                  active
                    ? side === 'BUY'
                      ? 'border-win/50 bg-win/15 text-win'
                      : 'border-loss/50 bg-loss/15 text-loss'
                    : 'border-obsidian-border bg-obsidian-bg text-obsidian-muted hover:text-slate-200'
                }`}
              >
                {side}
              </button>
            );
          })}
        </div>

        <EditableNumber
          value={entry.entry}
          onCommit={(v) => patch({ entry: v })}
          placeholder="Entry"
        />
        <EditableNumber
          value={entry.stop_loss}
          onCommit={(v) => patch({ stop_loss: v })}
          placeholder="Stop"
        />
        <EditableNumber
          value={entry.take_profit}
          onCommit={(v) => patch({ take_profit: v })}
          placeholder="Target"
        />
        <EditableNumber
          value={entry.quantity}
          onCommit={(v) => patch({ quantity: v })}
          placeholder="Shares"
        />
      </div>

      <div className="mt-2 flex flex-wrap items-center justify-between gap-x-4 gap-y-1.5">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] font-mono">
          <span className="text-obsidian-muted">{relativeAge(entry.created_at)}</span>

          {riskAmount !== null && (
            <span className="text-slate-300">
              risk <span className="text-slate-100">{money(riskAmount)}</span>
            </span>
          )}

          {tpScore !== null && (
            <span
              className={tpScore.isBackwards ? 'text-loss' : 'text-slate-300'}
            >
              {tpScore.rMultiple.toFixed(2)}R
              {tpScore.profit !== null && (
                <span className="ml-1 text-slate-100">
                  {money(tpScore.profit)}
                </span>
              )}
              {tpScore.isBackwards && ' — target is backwards'}
            </span>
          )}

          {sizing === null && entry.entry !== null && entry.stop_loss !== null && (
            <span className="text-loss">
              {sizingHint({
                side: entry.direction as Side,
                entry: entry.entry,
                stop: entry.stop_loss,
                accountSize: null,
                riskPercent: null,
              })}
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          {promoteError && (
            <span className="text-[10px] text-loss">{promoteError}</span>
          )}
          <button
            type="button"
            onClick={() => {
              setPromoteError(null);
              promoteMutation.mutate(entry.id, {
                onError: (err) => setPromoteError(err.message),
              });
            }}
            disabled={promoteMutation.isPending}
            title="Turn this note into a real plan"
            className="inline-flex items-center gap-1 rounded-lg border border-win-border bg-win-glow px-2.5 py-1 text-[10px] font-medium text-win transition-colors hover:bg-win/20 disabled:opacity-50"
          >
            {promoteMutation.isPending ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <ArrowUpRight className="h-3 w-3" />
            )}
            Promote to plan
          </button>
          <button
            type="button"
            onClick={() => deleteMutation.mutate(entry.id)}
            disabled={deleteMutation.isPending}
            aria-label={`Discard ${entry.ticker} note`}
            title="Discard"
            className="rounded-lg p-1.5 text-obsidian-muted transition-colors hover:text-loss disabled:opacity-50"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </div>
  );
};

interface QuickAddDraft {
  ticker: string;
  side: Side;
  entry: string;
  stop: string;
  takeProfit: string;
  quantity: string;
}

const BLANK_DRAFT: QuickAddDraft = {
  ticker: '',
  side: 'BUY',
  entry: '',
  stop: '',
  takeProfit: '',
  quantity: '',
};

/**
 * A fast note of what a trade might cost, for the moment there is no time to
 * open the Plan modal and write a real one. Deliberately separate from
 * `planned_trades` end to end — its own table, its own endpoints, nothing
 * here read by the matching engine or analytics — with `promote` as the one,
 * one-way bridge between the two. See migration 031 for the full reasoning.
 */
export const SizingScratchpad: React.FC = () => {
  const { data: settings } = useSettings();
  const { data: entries, isLoading, error } = useSizingScratchpad();
  const createMutation = useCreateSizingEntry();

  const [draft, setDraft] = useState<QuickAddDraft>(BLANK_DRAFT);
  const [formError, setFormError] = useState<string | null>(null);

  const patch = (fields: Partial<QuickAddDraft>) =>
    setDraft((prev) => ({ ...prev, ...fields }));

  const entryNum = toNullableNumber(draft.entry);
  const stopNum = toNullableNumber(draft.stop);
  const takeProfitNum = toNullableNumber(draft.takeProfit);
  const qtyNum = toNullableNumber(draft.quantity);

  const sizing = useMemo(
    () =>
      computeSizing({
        side: draft.side,
        entry: entryNum,
        stop: stopNum,
        accountSize: settings?.account_size ?? null,
        riskPercent: settings?.risk_percent ?? null,
      }),
    [draft.side, entryNum, stopNum, settings]
  );

  const hint = useMemo(
    () =>
      sizingHint({
        side: draft.side,
        entry: entryNum,
        stop: stopNum,
        accountSize: null,
        riskPercent: null,
      }),
    [draft.side, entryNum, stopNum]
  );

  const tpScore = useMemo(() => {
    if (entryNum === null || takeProfitNum === null || sizing === null) {
      return null;
    }
    return scoreTakeProfit({
      side: draft.side,
      entry: entryNum,
      takeProfit: takeProfitNum,
      riskPerShare: sizing.riskPerShare,
      shares: qtyNum ?? sizing.wholeShares,
    });
  }, [draft.side, entryNum, takeProfitNum, sizing, qtyNum]);

  const handleAdd = () => {
    const ticker = draft.ticker.trim().toUpperCase();
    if (!ticker) {
      setFormError('Ticker is required.');
      return;
    }
    setFormError(null);

    createMutation.mutate(
      {
        ticker,
        direction: draft.side,
        entry: entryNum,
        stop_loss: stopNum,
        take_profit: takeProfitNum,
        quantity: qtyNum,
      },
      {
        onSuccess: () => setDraft(BLANK_DRAFT),
        onError: (err) => setFormError(err.message),
      }
    );
  };

  return (
    <div className="space-y-6">
      {/* ---- Quick add ---- */}
      <div className="rounded-xl border border-obsidian-border bg-obsidian-card p-4">
        <div className="mb-3 flex items-center gap-2">
          <Calculator className="h-4 w-4 text-win" />
          <h2 className="text-sm font-semibold text-slate-200">
            Quick add
          </h2>
        </div>

        <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-6">
          <label className="block">
            <span className="text-[10px] uppercase tracking-wider text-obsidian-muted">
              Ticker
            </span>
            <input
              type="text"
              value={draft.ticker}
              onChange={(e) => patch({ ticker: e.target.value })}
              onKeyDown={(e) => e.key === 'Enter' && handleAdd()}
              placeholder="NVDA"
              maxLength={10}
              disabled={createMutation.isPending}
              className={`mt-1 ${inputClass} font-sans font-semibold uppercase`}
            />
          </label>

          <div>
            <span className="text-[10px] uppercase tracking-wider text-obsidian-muted">
              Side
            </span>
            <div className="mt-1 grid grid-cols-2 gap-1">
              {(['BUY', 'SELL'] as const).map((side) => {
                const active = draft.side === side;
                return (
                  <button
                    key={side}
                    type="button"
                    onClick={() => patch({ side })}
                    disabled={createMutation.isPending}
                    aria-pressed={active}
                    className={`rounded-lg border px-1.5 py-1.5 text-[11px] font-semibold transition-colors disabled:opacity-50 ${
                      active
                        ? side === 'BUY'
                          ? 'border-win/50 bg-win/15 text-win'
                          : 'border-loss/50 bg-loss/15 text-loss'
                        : 'border-obsidian-border bg-obsidian-bg text-obsidian-muted hover:text-slate-200'
                    }`}
                  >
                    {side}
                  </button>
                );
              })}
            </div>
          </div>

          <label className="block">
            <span className="text-[10px] uppercase tracking-wider text-obsidian-muted">
              Entry
            </span>
            <input
              type="number"
              inputMode="decimal"
              step="any"
              value={draft.entry}
              onChange={(e) => patch({ entry: e.target.value })}
              onKeyDown={(e) => e.key === 'Enter' && handleAdd()}
              placeholder="0.00"
              disabled={createMutation.isPending}
              className={`mt-1 ${inputClass}`}
            />
          </label>

          <label className="block">
            <span className="text-[10px] uppercase tracking-wider text-obsidian-muted">
              Stop
            </span>
            <input
              type="number"
              inputMode="decimal"
              step="any"
              value={draft.stop}
              onChange={(e) => patch({ stop: e.target.value })}
              onKeyDown={(e) => e.key === 'Enter' && handleAdd()}
              placeholder="0.00"
              disabled={createMutation.isPending}
              className={`mt-1 ${inputClass}`}
            />
          </label>

          <label className="block">
            <span className="text-[10px] uppercase tracking-wider text-obsidian-muted">
              Target
            </span>
            <input
              type="number"
              inputMode="decimal"
              step="any"
              value={draft.takeProfit}
              onChange={(e) => patch({ takeProfit: e.target.value })}
              onKeyDown={(e) => e.key === 'Enter' && handleAdd()}
              placeholder="0.00"
              disabled={createMutation.isPending}
              className={`mt-1 ${inputClass}`}
            />
          </label>

          <label className="block">
            <span className="text-[10px] uppercase tracking-wider text-obsidian-muted">
              Shares
            </span>
            <input
              type="number"
              inputMode="decimal"
              step="any"
              value={draft.quantity}
              onChange={(e) => patch({ quantity: e.target.value })}
              onKeyDown={(e) => e.key === 'Enter' && handleAdd()}
              placeholder={
                sizing?.wholeShares !== null && sizing?.wholeShares !== undefined
                  ? String(sizing.wholeShares)
                  : '0'
              }
              disabled={createMutation.isPending}
              className={`mt-1 ${inputClass}`}
            />
          </label>
        </div>

        {settings?.account_size === null && (
          <p className="mt-2 text-[10px] text-obsidian-muted">
            <Link href="/settings" className="text-slate-300 underline">
              Set your account size
            </Link>{' '}
            to get a suggested share count.
          </p>
        )}

        {/* Live preview — entirely client-side, the same computeSizing/
            scoreTakeProfit the Plan modal itself uses, so nothing is saved
            just to find out what a number means. */}
        {entryNum !== null && stopNum !== null && (
          <div className="mt-3 border-t border-obsidian-border pt-3">
            {sizing === null ? (
              <p className="text-[11px] text-loss">{hint}</p>
            ) : (
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] font-mono">
                <span className="text-obsidian-muted">
                  1R{' '}
                  <span className="text-slate-200">
                    {money(sizing.riskPerShare)}
                  </span>
                </span>
                {sizing.riskAmount !== null && (
                  <span className="text-obsidian-muted">
                    risk budget{' '}
                    <span className="text-slate-200">
                      {money(sizing.riskAmount)}
                    </span>
                  </span>
                )}
                {sizing.wholeShares !== null && (
                  <button
                    type="button"
                    onClick={() =>
                      patch({ quantity: String(sizing.wholeShares) })
                    }
                    className="text-obsidian-muted underline decoration-dotted hover:text-slate-200"
                  >
                    suggested{' '}
                    <span className="text-slate-200">
                      {sizing.wholeShares} sh
                    </span>
                  </button>
                )}
                {tpScore !== null && (
                  <span
                    className={
                      tpScore.isBackwards ? 'text-loss' : 'text-obsidian-muted'
                    }
                  >
                    at target{' '}
                    <span
                      className={tpScore.isBackwards ? 'text-loss' : 'text-slate-200'}
                    >
                      {tpScore.rMultiple.toFixed(2)}R
                      {tpScore.profit !== null &&
                        ` (${money(tpScore.profit)})`}
                    </span>
                    {tpScore.isBackwards && ' — backwards'}
                  </span>
                )}
              </div>
            )}
          </div>
        )}

        {formError && (
          <div className="mt-3 flex items-center text-xs text-loss">
            <AlertCircle className="mr-1.5 h-3.5 w-3.5" />
            {formError}
          </div>
        )}

        <div className="mt-3 flex justify-end">
          <button
            type="button"
            onClick={handleAdd}
            disabled={createMutation.isPending}
            className="inline-flex items-center gap-2 rounded-lg border border-win-border bg-win-glow px-4 py-2 text-xs font-medium text-win transition-colors hover:bg-win/20 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {createMutation.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Check className="h-3.5 w-3.5" />
            )}
            Add to scratchpad
          </button>
        </div>
      </div>

      {/* ---- Saved notes ---- */}
      <div>
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-obsidian-muted">
            Notes
          </h2>
          <p className="text-[10px] text-obsidian-muted">
            Cleared automatically after three days
          </p>
        </div>

        {isLoading && (
          <div className="flex items-center justify-center py-10 text-obsidian-muted">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            <span className="text-xs">Loading…</span>
          </div>
        )}

        {error && (
          <div className="flex items-start py-4 text-xs text-loss">
            <AlertCircle className="mr-1.5 h-4 w-4 shrink-0" />
            <span>
              {error instanceof Error
                ? error.message
                : 'Failed to load the scratchpad.'}
            </span>
          </div>
        )}

        {!isLoading && !error && (entries ?? []).length === 0 && (
          <p className="py-10 text-center text-xs text-obsidian-muted">
            Nothing noted yet. Add a ticker above.
          </p>
        )}

        <div className="space-y-2">
          {(entries ?? []).map((entry) => (
            <ScratchpadRow key={entry.id} entry={entry} />
          ))}
        </div>
      </div>
    </div>
  );
};

export default SizingScratchpad;

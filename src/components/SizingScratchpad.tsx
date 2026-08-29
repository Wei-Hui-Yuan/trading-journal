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
import { BreakevenCheck } from '@/components/BreakevenCheck';
import { ScaledExitPlanner } from '@/components/ScaledExitPlanner';
import { PositionSizingPanel } from '@/components/PositionSizingPanel';
import { formatUnsignedMoney } from '@/lib/format';
import {
  computeSizing,
  scoreTakeProfit,
  sizingHint,
  type ExitTranche,
  type Side,
} from '@/lib/positionSizing';
import type { SizingScratchpadEntry } from '@/types/sizing';

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

/**
 * One-click risk levels, either side of the usual 1%.
 *
 * Not a replacement for the free field beside them — the point is that
 * halving risk on a marginal setup should cost one click, or it does not
 * happen at the moment it matters.
 */
const RISK_PRESETS = [0.5, 1, 2] as const;

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

/**
 * The ticker field, committing on blur for the same reasons as the numbers.
 *
 * This was bound straight to server state and PATCHed on every `onChange`,
 * which made typing genuinely lossy rather than merely chatty. Each keystroke
 * fired a mutation whose success invalidated the list, so React re-rendered
 * with the ticker the server still had and reset the input to it — the
 * character just typed vanished until the round trip landed, and the next one
 * was typed into a field that was about to be overwritten again. Four letters
 * cost eight requests and rarely produced the four letters.
 *
 * Normalising on commit rather than per keystroke matters too: `.trim()` on
 * every change meant a space could never be typed at all, and upper-casing
 * mid-edit fought the caret. The CSS already renders the field uppercase, so
 * what is shown never changes — only what is stored, and when.
 */
const EditableTicker: React.FC<{
  value: string;
  onCommit: (next: string) => void;
  maxLength?: number;
  className?: string;
}> = ({ value, onCommit, maxLength, className }) => {
  const [text, setText] = useState(value);

  useEffect(() => {
    setText(value);
  }, [value]);

  const commit = () => {
    const next = text.trim().toUpperCase();
    // Show what was actually saved, not the keystrokes that produced it.
    setText(next);
    if (next !== value) onCommit(next);
  };

  return (
    <input
      type="text"
      value={text}
      maxLength={maxLength}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        // Enter is the other "I am done here" signal a text field gets for
        // free; blurring routes it through the one commit path.
        if (e.key === 'Enter') e.currentTarget.blur();
      }}
      className={className}
    />
  );
};

/**
 * What one saved note computes to, using the exact same library the Plan
 * modal does — the scratchpad and a real plan must never disagree about what
 * a given entry/stop/quantity means.
 *
 * The account size and risk % are handed in rather than read from settings
 * here, so a row is priced against the same risk the page header is currently
 * showing. Both used to be hard-coded null, which is why a saved note could
 * only ever report the risk of a quantity already typed and never suggest one.
 */
function useRowSizing(
  entry: SizingScratchpadEntry,
  accountSize: number | null,
  riskPercent: number | null
) {
  const side = entry.direction as Side;
  const sizing = useMemo(
    () =>
      computeSizing({
        side,
        entry: entry.entry,
        stop: entry.stop_loss,
        accountSize,
        riskPercent,
      }),
    [side, entry.entry, entry.stop_loss, accountSize, riskPercent]
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

const ScratchpadRow: React.FC<{
  entry: SizingScratchpadEntry;
  accountSize: number | null;
  riskPercent: number | null;
}> = ({ entry, accountSize, riskPercent }) => {
  const updateMutation = useUpdateSizingEntry();
  const deleteMutation = useDeleteSizingEntry();
  const promoteMutation = usePromoteSizingEntry();
  const [promoteError, setPromoteError] = useState<string | null>(null);

  const { sizing, riskAmount, tpScore } = useRowSizing(
    entry,
    accountSize,
    riskPercent
  );

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
        <EditableTicker
          value={entry.ticker}
          onCommit={(ticker) => patch({ ticker })}
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
              risk <span className="text-slate-100">{formatUnsignedMoney(riskAmount)}</span>
            </span>
          )}

          {/* Only worth offering while it would change something. A note
              already holding the suggested quantity does not need to be told
              to adopt it. */}
          {sizing?.wholeShares != null &&
            sizing.wholeShares > 0 &&
            sizing.wholeShares !== entry.quantity && (
              <button
                type="button"
                onClick={() => patch({ quantity: sizing.wholeShares })}
                className="text-obsidian-muted underline decoration-dotted transition-colors hover:text-slate-200"
              >
                suggested{' '}
                <span className="text-slate-200">{sizing.wholeShares} sh</span>
              </button>
            )}

          {tpScore !== null && (
            <span
              className={tpScore.isBackwards ? 'text-loss' : 'text-slate-300'}
            >
              {tpScore.rMultiple.toFixed(2)}R
              {tpScore.profit !== null && (
                <span className="ml-1 text-slate-100">
                  {formatUnsignedMoney(tpScore.profit)}
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
  /**
   * Risk for whatever is being sized right now, defaulting to the saved
   * account figure but editable — a lower-conviction setup gets sized smaller
   * without changing the default, the same way the Plan modal already allows.
   *
   * Held as an OVERRIDE rather than as seeded state. The obvious shape is
   * `useState('')` plus an effect that fills it in once settings arrive, but
   * that has to guard against clobbering a value typed while the request was
   * still in flight, and it trips `react-hooks/set-state-in-effect` for
   * exactly the cascading-render reason the rule exists. Deriving it needs no
   * effect and no guard: null means "nobody has chosen", which reads as the
   * account default the moment one is known, and any typed value — including
   * an empty string — wins from then on.
   *
   * Deliberately NOT persisted with the note. Nothing in the scratchpad table
   * records a risk %, and adding a column would make a scratch note carry a
   * claim about conviction it was never asked for.
   */
  const [riskOverride, setRiskOverride] = useState<string | null>(null);
  /**
   * Slices of a staged exit, at prices the trader picks.
   *
   * Client-only like the risk % beside it. The scratchpad table records what
   * a trade might cost; how it would be managed on the way out is a thought
   * about that trade, not part of the record, and giving it a column would
   * make every note carry a plan it was never asked for.
   */
  const [tranches, setTranches] = useState<ExitTranche[]>([]);
  const riskPercentText =
    riskOverride ?? (settings ? String(settings.risk_percent) : '');

  const patch = (fields: Partial<QuickAddDraft>) =>
    setDraft((prev) => ({ ...prev, ...fields }));

  const entryNum = toNullableNumber(draft.entry);
  const stopNum = toNullableNumber(draft.stop);
  const takeProfitNum = toNullableNumber(draft.takeProfit);
  const qtyNum = toNullableNumber(draft.quantity);

  const accountSize = settings?.account_size ?? null;
  const riskPercentNum = toNullableNumber(riskPercentText);

  const sizing = useMemo(
    () =>
      computeSizing({
        side: draft.side,
        entry: entryNum,
        stop: stopNum,
        accountSize,
        riskPercent: riskPercentNum,
      }),
    [draft.side, entryNum, stopNum, accountSize, riskPercentNum]
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

        {/* The two figures the sizing answers against, side by side, so it is
            never ambiguous which balance and which conviction produced the
            share count below. Account size is read-only here: it belongs to
            the account rather than to a trade, so it is set once on /settings
            instead of retyped for every idea. */}
        <div className="mt-3 grid grid-cols-1 gap-2.5 border-t border-obsidian-border pt-3 sm:grid-cols-2">
          <div>
            <span className="text-[10px] uppercase tracking-wider text-obsidian-muted">
              Account
            </span>
            <div className="mt-1 flex h-[34px] items-center justify-between rounded-lg border border-obsidian-border bg-obsidian-bg/60 px-2.5">
              <span className="font-mono text-xs text-slate-300">
                {accountSize === null
                  ? 'not set'
                  : formatUnsignedMoney(accountSize)}
              </span>
              <Link
                href="/settings"
                className="text-[10px] text-obsidian-muted transition-colors hover:text-slate-200"
              >
                Edit
              </Link>
            </div>
          </div>

          <div>
            <span className="text-[10px] uppercase tracking-wider text-obsidian-muted">
              Risk this trade
            </span>
            <div className="mt-1 flex h-[34px] items-center gap-1">
              {RISK_PRESETS.map((preset) => {
                const active = riskPercentNum === preset;
                return (
                  <button
                    key={preset}
                    type="button"
                    onClick={() => setRiskOverride(String(preset))}
                    aria-pressed={active}
                    className={`h-full rounded-lg border px-2 text-[10px] font-semibold transition-colors ${
                      active
                        ? 'border-win/50 bg-win/15 text-win'
                        : 'border-obsidian-border bg-obsidian-bg text-obsidian-muted hover:text-slate-200'
                    }`}
                  >
                    {preset}%
                  </button>
                );
              })}
              {/* 0.01, matching the Plan modal's own field and the two
                  decimals trades.risk_percent keeps. `any` would accept a
                  1.234 that is not representable downstream. */}
              <input
                type="number"
                inputMode="decimal"
                step="0.01"
                min="0"
                value={riskPercentText}
                onChange={(e) => setRiskOverride(e.target.value)}
                aria-label="Risk percent for this trade"
                placeholder="1"
                className={`${inputClass} h-full flex-1`}
              />
            </div>
          </div>
        </div>

        {settings?.account_size === null && (
          <p className="mt-2 text-[10px] text-obsidian-muted">
            <Link href="/settings" className="text-slate-300 underline">
              Set your account size
            </Link>{' '}
            to get a suggested share count. Target prices work without it.
          </p>
        )}

        {/* Live preview — entirely client-side, and the same panel the Plan
            modal renders, so nothing has to be saved just to find out what a
            number means. */}
        {entryNum !== null && stopNum !== null && (
          <div className="mt-3 border-t border-obsidian-border pt-3">
            <PositionSizingPanel
              sizing={sizing}
              hint={hint}
              side={draft.side}
              takeProfit={takeProfitNum}
              takeProfitScore={tpScore}
              enteredQty={qtyNum}
              accountSize={accountSize}
              onPickTarget={(picked) => patch({ takeProfit: picked })}
              onUseShares={(shares) => patch({ quantity: String(shares) })}
              disabled={createMutation.isPending}
            />

            {/* Scored against the target actually typed, not the ladder --
                the ladder offers four answers and this question only has
                meaning once one of them has been chosen. */}
            {tpScore !== null && !tpScore.isBackwards && (
              <div className="mt-2.5">
                <BreakevenCheck rMultiple={tpScore.rMultiple} />
              </div>
            )}

            {/* Below the single-target scorecard, because it answers the
                question you reach only after that one: not "what is this
                target worth" but "what does the whole way out average". */}
            {sizing !== null && entryNum !== null && (
              <div className="mt-2.5">
                <ScaledExitPlanner
                  side={draft.side}
                  entry={entryNum}
                  riskPerShare={sizing.riskPerShare}
                  shares={qtyNum ?? sizing.wholeShares}
                  tranches={tranches}
                  onChange={setTranches}
                />
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
            <ScratchpadRow
              key={entry.id}
              entry={entry}
              accountSize={accountSize}
              riskPercent={riskPercentNum}
            />
          ))}
        </div>
      </div>
    </div>
  );
};

export default SizingScratchpad;

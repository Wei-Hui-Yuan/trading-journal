'use client';

import React from 'react';

import { formatPrice, formatUnsignedMoney } from '@/lib/format';
import {
  computePlannedRisk,
  type Side,
  type SizingResult,
  type TakeProfitScore,
} from '@/lib/positionSizing';

/**
 * What a sized position works out to — the outputs half of the calculator.
 *
 * `computeSizing` was already shared between the Plan modal and the sizing
 * scratchpad; the rendering of its result was not. The modal grew the full
 * treatment (share count, cost, the R ladder, warnings) and the scratchpad
 * grew a four-value strip that discarded most of the same result object,
 * including the ladder entirely — it still carried a dead `price` formatter
 * from a ladder that was scoped and never built.
 *
 * Two surfaces that disagree about what a trade risks is the one failure this
 * calculator cannot be allowed to have, so the answer is rendered in exactly
 * one place.
 *
 * INPUTS ARE DELIBERATELY NOT HERE. Entry, stop, quantity and risk % live in
 * whichever form is hosting this — one is a modal with validation and a submit
 * payload, the other is a scratchpad row that PATCHes on blur — and they have
 * no business being unified. This component takes a `SizingResult` and renders
 * it. The host owns the fields that produced it.
 */
export interface PositionSizingPanelProps {
  /** Null when the inputs cannot support a result; `hint` says why. */
  sizing: SizingResult | null;
  /** Why sizing is unavailable, from `sizingHint`. Shown in place of outputs. */
  hint: string | null;
  side: Side;
  /** The take profit currently typed into the host's own field. */
  takeProfit: number | null;
  /** That take profit's worth, from `scoreTakeProfit`. */
  takeProfitScore: TakeProfitScore | null;
  /**
   * The quantity the host has typed, or null if it is empty. Only used to say
   * whether a profit figure is quoted on a real quantity or on the
   * calculator's own suggestion.
   */
  enteredQty: number | null;
  /**
   * Receives the target price ALREADY FORMATTED, because the hosts hold their
   * prices as strings and `formatPrice` is what decides how many decimals a
   * sub-dollar ticker needs. Handing back a raw number would put
   * `0.12345678999` into a field the user is about to read.
   */
  onPickTarget: (formattedPrice: string) => void;
  /**
   * Needed to express the planned risk as a percent of the account. Absent
   * or null still shows the dollar figure, which is the more important half.
   */
  accountSize?: number | null;
  /**
   * Appended to the planned-risk line by a host that does something further
   * with the figure — the Plan modal stores it, a scratchpad note does not.
   */
  plannedRiskNote?: string;
  /** Omitted where there is no quantity field to fill. */
  onUseShares?: (shares: number) => void;
  /**
   * The button's wording, which differs by host.
   *
   * NOT `useSharesLabel`, however naturally that reads. The `use` prefix makes
   * `react-hooks/rules-of-hooks` classify it as a hook, and the panel returns
   * early when there is nothing to size — so calling it below that return is
   * reported as a conditionally-called hook and fails lint.
   */
  formatSharesLabel?: (shares: number) => string;
  disabled?: boolean;
}

export const PositionSizingPanel: React.FC<PositionSizingPanelProps> = ({
  sizing,
  hint,
  side,
  takeProfit,
  takeProfitScore,
  enteredQty,
  accountSize = null,
  plannedRiskNote,
  onPickTarget,
  onUseShares,
  formatSharesLabel = (n) => `Use ${n} share${n === 1 ? '' : 's'}`,
  disabled = false,
}) => {
  // A reason is shown rather than an empty panel — an inverted stop is a
  // mistake worth naming, not hiding.
  if (sizing === null) {
    return <p className="text-[10px] text-obsidian-muted">{hint}</p>;
  }

  // What the chosen quantity risks, which is not the budget above it: the
  // share count is floored, so a $25 budget at $10 a share buys two shares
  // risking $20. Follows the entered quantity rather than the suggestion,
  // because taking half size is a deliberate act.
  const plannedRisk = computePlannedRisk({
    riskPerShare: sizing.riskPerShare,
    shares: enteredQty,
    accountSize,
  });

  return (
    <div className="space-y-2.5">
      <div className="grid grid-cols-2 gap-x-3 gap-y-1.5 text-[11px]">
        <div className="flex justify-between">
          <span className="text-obsidian-muted">1R / share</span>
          <span className="font-mono text-slate-200">
            {formatUnsignedMoney(sizing.riskPerShare)}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-obsidian-muted">Risk budget</span>
          <span className="font-mono text-slate-200">
            {sizing.riskAmount === null
              ? '—'
              : formatUnsignedMoney(sizing.riskAmount)}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-obsidian-muted">Shares</span>
          <span className="font-mono text-slate-200">
            {sizing.wholeShares === null ? '—' : sizing.wholeShares}
            {sizing.exactShares !== null && (
              <span className="ml-1 text-obsidian-muted">
                ({sizing.exactShares.toFixed(2)})
              </span>
            )}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-obsidian-muted">Cost</span>
          <span className="font-mono text-slate-200">
            {sizing.positionCost === null
              ? '—'
              : formatUnsignedMoney(sizing.positionCost)}
            {sizing.accountFraction !== null && (
              <span
                className={`ml-1 ${
                  sizing.accountFraction > 1 ? 'text-loss' : 'text-obsidian-muted'
                }`}
              >
                ({(sizing.accountFraction * 100).toFixed(0)}%)
              </span>
            )}
          </span>
        </div>
      </div>

      {/* Above 100% of the account the position needs margin. Not an error —
          the account has it — but it should be a decision rather than a
          surprise noticed after the fill. */}
      {sizing.accountFraction !== null && sizing.accountFraction > 1 && (
        <p className="text-[10px] text-loss">
          Costs more than the account holds — needs margin.
        </p>
      )}

      {sizing.wholeShares === 0 && (
        <p className="text-[10px] text-loss">
          Risk budget is smaller than one share&apos;s risk. Widen the account
          size, raise the risk %, or tighten the stop.
        </p>
      )}

      {/* The R ladder. Clicking one writes it into the host's take profit
          field, because a plan stores a single target — these are the
          options, and the field records which was chosen. */}
      <div>
        <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
          Take Profit Targets
        </span>
        <div className="mt-1 grid grid-cols-2 gap-2 sm:grid-cols-4">
          {sizing.targets.map((t) => {
            const chosen =
              takeProfit !== null && Math.abs(takeProfit - t.price) < 0.005;
            return (
              <button
                key={t.r}
                type="button"
                onClick={() => onPickTarget(formatPrice(t.price))}
                disabled={disabled}
                aria-pressed={chosen}
                // The visible label is three separate spans of numbers, which
                // reads as an unnamed button to a screen reader. Spelled out
                // here instead.
                aria-label={`Set take profit to ${formatPrice(t.price)} (${t.r}R)`}
                className={`rounded-lg border px-2 py-1.5 text-left transition-colors disabled:opacity-50 ${
                  chosen
                    ? 'border-win/50 bg-win/15'
                    : 'border-obsidian-border bg-obsidian-bg hover:border-slate-600'
                }`}
              >
                <span
                  className={`block text-[10px] font-semibold ${
                    chosen ? 'text-win' : 'text-obsidian-muted'
                  }`}
                >
                  {t.r}R
                </span>
                <span className="block font-mono text-[11px] text-slate-200">
                  {formatPrice(t.price)}
                </span>
                {t.profit !== null && (
                  <span className="block font-mono text-[10px] text-obsidian-muted">
                    +{formatUnsignedMoney(t.profit)}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </div>

      {/* What the take profit in the host's own field is worth. The ladder
          answers "where is 2R?"; this answers the question you actually
          arrive with — "I want out at 25, what does that pay?" — which
          otherwise means eyeballing where 25 falls between two chips and
          interpolating. */}
      {takeProfitScore !== null && (
        <div
          className={`rounded-lg border px-2.5 py-2 ${
            takeProfitScore.isBackwards
              ? 'border-loss/40 bg-loss/5'
              : 'border-obsidian-border bg-obsidian-bg/60'
          }`}
        >
          {takeProfitScore.isBackwards ? (
            // Named as the typo it is rather than rendered as a negative R,
            // which would read like a deliberate choice.
            <p className="text-[10px] leading-relaxed text-loss">
              Take profit {takeProfit === null ? '' : formatPrice(takeProfit)} is
              on the losing side of your entry
              {side === 'BUY'
                ? ' — for a long it has to sit above it.'
                : ' — for a short it has to sit below it.'}
            </p>
          ) : (
            <>
              <div className="flex items-baseline justify-between">
                <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
                  Your take profit
                </span>
                <span className="font-mono text-[11px] text-slate-200">
                  {takeProfitScore.rMultiple.toFixed(2)}R
                  {takeProfitScore.profit !== null && (
                    <span className="ml-1.5 text-win">
                      +{formatUnsignedMoney(takeProfitScore.profit)}
                    </span>
                  )}
                </span>
              </div>
              <p className="mt-0.5 text-[10px] text-obsidian-muted">
                {formatUnsignedMoney(takeProfitScore.perShare)} per share
                {takeProfitScore.shares !== null &&
                  ` on ${takeProfitScore.shares} share${
                    takeProfitScore.shares === 1 ? '' : 's'
                  }`}
                {enteredQty === null &&
                  takeProfitScore.shares !== null &&
                  ' (suggested size)'}
              </p>
            </>
          )}
        </div>
      )}

      {onUseShares !== undefined &&
        sizing.wholeShares !== null &&
        sizing.wholeShares > 0 && (
          <button
            type="button"
            onClick={() => onUseShares(sizing.wholeShares as number)}
            disabled={disabled}
            className="w-full rounded-lg border border-obsidian-border bg-obsidian-bg px-3 py-1.5 text-[11px] text-slate-300 transition-colors hover:border-slate-600 hover:text-slate-100 disabled:opacity-50"
          >
            {formatSharesLabel(sizing.wholeShares)}
          </button>
        )}

      {/* The number actually on the line. Without it the panel shows a risk
          budget the position does not spend — a $25 budget against two
          shares risking $20 overstates the exposure by a fifth, and it is
          the budget that is the less useful of the two. */}
      {plannedRisk !== null && (
        <p className="text-[10px] text-obsidian-muted">
          Planning {enteredQty} share{enteredQty === 1 ? '' : 's'} — risking{' '}
          <span className="text-slate-300">
            {formatUnsignedMoney(plannedRisk.amount)}
          </span>
          {plannedRisk.percent !== null &&
            ` (${plannedRisk.percent.toFixed(2)}% of account)`}
          .{plannedRiskNote ? ` ${plannedRiskNote}` : ''}
        </p>
      )}
    </div>
  );
};

export default PositionSizingPanel;

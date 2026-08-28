'use client';

import React from 'react';

import { useAdvancedMetrics } from '@/hooks/useTradeInbox';
import { breakevenWinRate } from '@/lib/positionSizing';

/**
 * How often a target has to work, against how often the trader's own targets
 * actually have.
 *
 * The calculator can say a 3R target pays $60. It cannot, on its own, say
 * whether 3R is a target this trader reaches — and "needs 25% to break even"
 * is only useful next to the rate actually achieved. Both halves are already
 * in the app; nothing here computes new history, it reads
 * `/api/analytics/advanced` and puts the two figures side by side.
 *
 * Deliberately descriptive. It reports arithmetic and the trader's own
 * record; it does not grade the trade or recommend taking it.
 */

/**
 * Below this many scored trades, a win rate is noise dressed as a number.
 *
 * Not a threshold for hiding anything — the figure and its sample are always
 * shown. It only decides whether the sample is called out as thin, because a
 * 40% win rate over five trades and over five hundred are different claims
 * and the screen should not render them identically.
 */
const THIN_SAMPLE = 20;

export const BreakevenCheck: React.FC<{
  /** The R the target in question is worth. Null when there is no target. */
  rMultiple: number | null;
}> = ({ rMultiple }) => {
  // All-time rather than the Analytics page's selected window: this is being
  // read while sizing a trade, where "how often does this work for me" is a
  // question about the whole record, not about a date range chosen elsewhere.
  const { data: metrics, isPending } = useAdvancedMetrics();

  if (rMultiple === null) return null;
  const needed = breakevenWinRate(rMultiple);
  // Null for a backwards target. The panel already names that as the typo it
  // is; repeating it here as a win-rate problem would misdescribe it.
  if (needed === null) return null;

  /*
   * `scored_trades`, NOT `win_rate_pct`, is what decides whether there is
   * history to compare against.
   *
   * The endpoint returns `win_rate_pct: 0.0` when nothing could be scored --
   * unlike `avg_r`, which correctly returns null. Reading the rate alone, an
   * empty journal is indistinguishable from a trader who has lost every
   * trade, and rendering "you: 0%" would state the second while meaning the
   * first. The population is the only field that separates them.
   */
  const scored = metrics?.scored_trades ?? 0;
  const actual = metrics !== undefined && scored > 0 ? metrics.win_rate_pct : null;

  return (
    <div className="rounded-lg border border-obsidian-border bg-obsidian-bg/60 px-2.5 py-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
          Breaks even at
        </span>
        <span className="font-mono text-[11px] text-slate-200">
          {needed.toFixed(0)}%
          <span className="ml-1 text-obsidian-muted">
            of {rMultiple.toFixed(2)}R trades
          </span>
        </span>
      </div>

      <p className="mt-0.5 text-[10px] leading-relaxed text-obsidian-muted">
        {/* Nothing at all while the request is in flight. "No scored history"
            is a claim about the journal, and making it before the answer has
            arrived is the same error as reading the 0% -- stating something
            true of the response rather than of the record. The floor above is
            knowable immediately and stays on screen. */}
        {isPending ? null : actual === null ? (
          // Said plainly rather than shown as a zero. There is no record yet,
          // which is not the same as a record of losing.
          <>No scored history yet to compare against.</>
        ) : (
          <>
            You win{' '}
            <span className={actual >= needed ? 'text-win' : 'text-slate-300'}>
              {actual.toFixed(0)}%
            </span>{' '}
            across {scored} scored trade{scored === 1 ? '' : 's'}
            {scored < THIN_SAMPLE && ' — a thin sample'}.
          </>
        )}{' '}
        {/* The floor assumes every loss is a full 1R and every win reaches
            the target exactly, and counts no commission or slippage. The
            journal measures real slippage separately because it is not zero,
            so the true bar always sits above this one. */}
        Before costs.
      </p>
    </div>
  );
};

export default BreakevenCheck;

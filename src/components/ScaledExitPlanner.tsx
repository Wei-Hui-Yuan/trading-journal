'use client';

import React from 'react';
import { Plus, Trash2 } from 'lucide-react';

import { formatPrice, formatUnsignedMoney } from '@/lib/format';
import { planScaledExit, type ExitTranche, type Side } from '@/lib/positionSizing';

/**
 * Several slices out, at prices of the trader's own choosing.
 *
 * The R ladder answers "where is 2R?" and the take-profit scorecard answers
 * "what is 25 worth?". Neither can answer the question a position is actually
 * managed by — "half off at 160, a quarter at 175, let the rest run" — and the
 * blended result is not something the other two can be eyeballed into.
 *
 * Prices are free-form rather than picked off the ladder on purpose: a real
 * scale-out is placed at structure — a prior high, a round number, the
 * measured move — not at whatever price 2R happens to land on.
 *
 * Client-only. Nothing here is persisted, matching the rest of the
 * scratchpad's sizing controls: a note records what a trade might cost, and a
 * staged exit is a thought about managing it rather than part of that record.
 */

const BLANK: ExitTranche = { percent: 0, price: 0 };

const cellClass =
  'w-full rounded border border-obsidian-border bg-obsidian-bg px-2 py-1 text-right ' +
  'font-mono text-[11px] text-slate-100 placeholder:text-slate-700 ' +
  'focus:border-slate-600 focus:outline-none';

const ROW = 'grid grid-cols-[64px_1fr_58px_1fr_24px] items-center gap-1.5';

export const ScaledExitPlanner: React.FC<{
  side: Side;
  entry: number;
  riskPerShare: number;
  /** The quantity being planned, or null when unsized. */
  shares: number | null;
  tranches: ExitTranche[];
  onChange: (next: ExitTranche[]) => void;
}> = ({ side, entry, riskPerShare, shares, tranches, onChange }) => {
  const plan = planScaledExit({ side, entry, riskPerShare, shares, tranches });

  const patch = (index: number, field: keyof ExitTranche, raw: string) => {
    const value = raw.trim() === '' ? 0 : Number(raw);
    onChange(
      tranches.map((t, i) =>
        i === index ? { ...t, [field]: Number.isFinite(value) ? value : 0 } : t
      )
    );
  };

  const overAllocated = (plan?.allocatedPercent ?? 0) > 100;

  return (
    <div className="rounded-lg border border-obsidian-border bg-obsidian-bg/60 px-2.5 py-2">
      <div className="flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
          Scaled exit
        </span>
        <button
          type="button"
          onClick={() => onChange([...tranches, BLANK])}
          className="inline-flex items-center gap-1 text-[10px] text-obsidian-muted underline decoration-dotted transition-colors hover:text-slate-200"
        >
          <Plus className="h-3 w-3" />
          Add a slice
        </button>
      </div>

      {tranches.length === 0 ? (
        <p className="mt-1 text-[10px] text-obsidian-muted">
          Take some off at one price and the rest at another, and see what the
          whole plan averages.
        </p>
      ) : (
        <>
          <div
            className={`mt-1.5 ${ROW} text-[9px] uppercase tracking-wide text-obsidian-muted`}
          >
            <span>% out</span>
            <span className="text-right">at price</span>
            <span className="text-right">R</span>
            <span className="text-right">shares · gain</span>
            <span />
          </div>

          <div className="mt-1 space-y-1">
            {tranches.map((tranche, i) => {
              const leg = plan?.legs.find(
                (l) => l.percent === tranche.percent && l.price === tranche.price
              );
              return (
                <div key={i} className={ROW}>
                  <input
                    type="number"
                    step="any"
                    min="0"
                    max="100"
                    value={tranche.percent || ''}
                    placeholder="%"
                    aria-label={`Percent out, slice ${i + 1}`}
                    onChange={(e) => patch(i, 'percent', e.target.value)}
                    className={cellClass}
                  />
                  <input
                    type="number"
                    step="any"
                    min="0"
                    value={tranche.price || ''}
                    placeholder="price"
                    aria-label={`Exit price, slice ${i + 1}`}
                    onChange={(e) => patch(i, 'price', e.target.value)}
                    className={cellClass}
                  />
                  <span
                    className={`text-right font-mono text-[11px] ${
                      leg?.isBackwards ? 'text-loss' : 'text-slate-200'
                    }`}
                  >
                    {leg ? `${leg.rMultiple.toFixed(2)}R` : '—'}
                  </span>
                  <span className="text-right font-mono text-[10px] text-obsidian-muted">
                    {leg && leg.shares !== null ? (
                      <>
                        <span className={leg.shares === 0 ? 'text-loss' : ''}>
                          {leg.shares} sh
                        </span>
                        {leg.profit !== null &&
                          ` · ${formatUnsignedMoney(leg.profit)}`}
                      </>
                    ) : (
                      '—'
                    )}
                  </span>
                  <button
                    type="button"
                    onClick={() => onChange(tranches.filter((_, j) => j !== i))}
                    aria-label={`Remove slice ${i + 1}`}
                    className="rounded p-0.5 text-obsidian-muted transition-colors hover:text-loss"
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                </div>
              );
            })}
          </div>

          {plan !== null && plan.legs.length > 0 && (
            <div className="mt-2 border-t border-obsidian-border pt-1.5">
              <div className="flex items-baseline justify-between">
                <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
                  Blended
                </span>
                <span className="font-mono text-[11px] text-slate-200">
                  {plan.blendedR === null ? '—' : `${plan.blendedR.toFixed(2)}R`}
                  {plan.totalProfit !== null && (
                    <span className="ml-1.5 text-win">
                      +{formatUnsignedMoney(plan.totalProfit)}
                    </span>
                  )}
                </span>
              </div>

              {/* The blend is normalised to what has been allocated, so the
                  remainder has to be stated or the figure reads as the whole
                  position's outcome. */}
              <p className="mt-0.5 text-[10px] text-obsidian-muted">
                across {plan.allocatedPercent}% of the position
                {plan.unallocatedPercent > 0 &&
                  ` — ${plan.unallocatedPercent}% still running`}
                .
              </p>

              {overAllocated && (
                <p className="mt-0.5 text-[10px] text-loss">
                  That is {plan.allocatedPercent}% of a position you only hold
                  100% of.
                </p>
              )}

              {plan.hasEmptyLeg && (
                <p className="mt-0.5 text-[10px] text-loss">
                  A slice this small sells no whole shares
                  {shares !== null && ` at ${shares} share${shares === 1 ? '' : 's'}`}
                  . Scaling out needs a position big enough to divide.
                </p>
              )}

              {/* Flooring each leg strands shares that no slice claims. On a
                  small position that is most of it. */}
              {plan.residualShares !== null && plan.residualShares > 0 && (
                <p className="mt-0.5 text-[10px] text-obsidian-muted">
                  {plan.residualShares} share
                  {plan.residualShares === 1 ? '' : 's'} left over at{' '}
                  {formatPrice(entry)}.
                </p>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
};

export default ScaledExitPlanner;

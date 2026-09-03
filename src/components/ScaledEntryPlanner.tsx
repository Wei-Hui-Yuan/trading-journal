'use client';

import React from 'react';
import { Plus, Trash2 } from 'lucide-react';

import { formatPrice, formatUnsignedMoney } from '@/lib/format';
import {
  planScaledEntry,
  type EntryTranche,
  type Side,
} from '@/lib/positionSizing';

/**
 * Several buys on the way into one position, against one stop and one budget.
 *
 * The case this exists for: "Entry 1 entered / Entry 2 226 / Entry 3 216 /
 * Hard sl 210 / Total risking 1.5%". Sizing that by hand is genuinely hard
 * rather than tedious — each rung sits a different distance from the shared
 * stop, so the three share counts are three different numbers that have to
 * add up to one risk figure, and the arithmetic that gets it wrong looks
 * exactly as plausible as the arithmetic that gets it right.
 *
 * The mirror of `ScaledExitPlanner` in shape, and deliberately not in
 * substance: an exit divides a position that already exists, while an entry is
 * solved BACKWARDS from the budget. See `planScaledEntry`.
 *
 * WHAT THIS DOES NOT DO. It never writes to the host form on its own. A
 * laddered plan is stored as one blended entry and one quantity — losslessly,
 * see `EntryBlend.entry` — but which numbers land in the form is a decision,
 * so it takes one click on the button at the bottom. That is the same idiom
 * the R-ladder chips and "Use N shares" already use, and it keeps the single
 * entry field the one thing that decides what gets saved.
 */

const BLANK: EntryTranche = { percent: 0, price: 0 };

const cellClass =
  'w-full rounded border border-obsidian-border bg-obsidian-bg px-2 py-1 text-right ' +
  'font-mono text-[11px] text-slate-100 placeholder:text-slate-700 ' +
  'focus:border-slate-600 focus:outline-none';

const ROW = 'grid grid-cols-[64px_1fr_58px_1fr_24px] items-center gap-1.5';

export const ScaledEntryPlanner: React.FC<{
  side: Side;
  /** The one hard stop every rung is measured against. */
  stop: number | null;
  accountSize: number | null;
  riskPercent: number | null;
  tranches: EntryTranche[];
  onChange: (next: EntryTranche[]) => void;
  /**
   * Writes the blend into the host's entry and quantity fields. The price
   * arrives ALREADY FORMATTED, for the same reason `onPickTarget` does: the
   * hosts hold prices as strings, and handing back 229.42631578947367 would
   * put that into a field the user is about to read — and into a column that
   * only holds four decimal places.
   */
  onApply?: (entry: string, shares: number) => void;
  disabled?: boolean;
}> = ({
  side,
  stop,
  accountSize,
  riskPercent,
  tranches,
  onChange,
  onApply,
  disabled = false,
}) => {
  const plan = planScaledEntry({
    side,
    stop,
    accountSize,
    riskPercent,
    tranches,
  });

  const patch = (index: number, field: keyof EntryTranche, raw: string) => {
    const value = raw.trim() === '' ? 0 : Number(raw);
    onChange(
      tranches.map((t, i) =>
        i === index ? { ...t, [field]: Number.isFinite(value) ? value : 0 } : t
      )
    );
  };

  const overAllocated = (plan?.allocatedPercent ?? 0) > 100;
  const underAllocated =
    plan !== null && plan.legs.length > 0 && plan.allocatedPercent < 100;
  // What the FIRST rung alone risks. A ladder is a conditional plan: the
  // screenshot that prompted this says "Entry 1 entered" with two rungs still
  // resting, so the 1.5% it claims is what it risks only if all three fill.
  // Stating both is the difference between a plan and a hope.
  const firstLeg = plan?.legs[0];
  const partial =
    plan !== null &&
    plan.legs.length > 1 &&
    firstLeg !== undefined &&
    firstLeg.riskAmount !== null
      ? {
          amount: firstLeg.riskAmount,
          percent:
            accountSize !== null && accountSize > 0
              ? (firstLeg.riskAmount / accountSize) * 100
              : null,
        }
      : null;

  return (
    <div className="rounded-lg border border-obsidian-border bg-obsidian-bg/60 px-2.5 py-2">
      <div className="flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
          Scaled entry
        </span>
        <button
          type="button"
          onClick={() => onChange([...tranches, BLANK])}
          disabled={disabled}
          className="inline-flex items-center gap-1 text-[10px] text-obsidian-muted underline decoration-dotted transition-colors hover:text-slate-200 disabled:opacity-50"
        >
          <Plus className="h-3 w-3" />
          Add a rung
        </button>
      </div>

      {tranches.length === 0 ? (
        <p className="mt-1 text-[10px] text-obsidian-muted">
          Buy part of the position at one price and the rest lower, and see the
          share count that keeps the whole ladder inside one risk budget.
        </p>
      ) : (
        <>
          <div
            className={`mt-1.5 ${ROW} text-[9px] uppercase tracking-wide text-obsidian-muted`}
          >
            <span>% in</span>
            <span className="text-right">at price</span>
            <span className="text-right">1R/sh</span>
            <span className="text-right">shares · risk</span>
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
                    aria-label={`Percent in, rung ${i + 1}`}
                    onChange={(e) => patch(i, 'percent', e.target.value)}
                    disabled={disabled}
                    className={cellClass}
                  />
                  <input
                    type="number"
                    step="any"
                    min="0"
                    value={tranche.price || ''}
                    placeholder="price"
                    aria-label={`Entry price, rung ${i + 1}`}
                    onChange={(e) => patch(i, 'price', e.target.value)}
                    disabled={disabled}
                    className={cellClass}
                  />
                  <span
                    className={`text-right font-mono text-[11px] ${
                      leg?.isBackwards ? 'text-loss' : 'text-slate-200'
                    }`}
                  >
                    {leg ? formatUnsignedMoney(Math.abs(leg.riskPerShare)) : '—'}
                  </span>
                  <span className="text-right font-mono text-[10px] text-obsidian-muted">
                    {leg && leg.shares !== null ? (
                      <>
                        <span className={leg.shares === 0 ? 'text-loss' : ''}>
                          {leg.shares} sh
                        </span>
                        {leg.riskAmount !== null &&
                          ` · ${formatUnsignedMoney(leg.riskAmount)}`}
                      </>
                    ) : (
                      '—'
                    )}
                  </span>
                  <button
                    type="button"
                    onClick={() => onChange(tranches.filter((_, j) => j !== i))}
                    aria-label={`Remove rung ${i + 1}`}
                    disabled={disabled}
                    className="rounded p-0.5 text-obsidian-muted transition-colors hover:text-loss disabled:opacity-50"
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                </div>
              );
            })}
          </div>

          {plan === null ? (
            <p className="mt-2 border-t border-obsidian-border pt-1.5 text-[10px] text-obsidian-muted">
              Enter a stop above — every rung is priced against it, so there is
              nothing to size a ladder from without one.
            </p>
          ) : (
            plan.blend !== null && (
              <div className="mt-2 border-t border-obsidian-border pt-1.5">
                <div className="flex items-baseline justify-between">
                  <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
                    Blended entry
                  </span>
                  <span className="font-mono text-[11px] text-slate-200">
                    {formatPrice(plan.blend.entry)}
                    {plan.totalShares !== null && (
                      <span className="ml-1.5 text-obsidian-muted">
                        × {plan.totalShares} sh
                      </span>
                    )}
                  </span>
                </div>

                {/* The number the ladder was actually asked for, against the
                    budget it was sized against. Flooring twice always lands
                    under, and the shortfall is worth seeing rather than
                    inferring from a share count. */}
                {plan.totalRisk !== null && (
                  <div className="mt-0.5 flex items-baseline justify-between">
                    <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
                      Risk, all rungs filled
                    </span>
                    <span className="font-mono text-[11px] text-slate-200">
                      {formatUnsignedMoney(plan.totalRisk)}
                      {plan.totalRiskPercent !== null &&
                        ` (${plan.totalRiskPercent.toFixed(2)}%)`}
                      {plan.riskBudget !== null && (
                        <span className="ml-1.5 text-obsidian-muted">
                          of {formatUnsignedMoney(plan.riskBudget)}
                        </span>
                      )}
                    </span>
                  </div>
                )}

                {/* A ladder is a conditional. Without this line the plan
                    claims a risk it only reaches if every rung fills. */}
                {partial !== null && (
                  <p className="mt-0.5 text-[10px] text-obsidian-muted">
                    If only rung 1 fills:{' '}
                    <span className="text-slate-300">
                      {formatUnsignedMoney(partial.amount)}
                    </span>
                    {partial.percent !== null &&
                      ` (${partial.percent.toFixed(2)}%)`}
                    .
                  </p>
                )}

                {plan.hasBackwardsLeg && (
                  <p className="mt-0.5 text-[10px] text-loss">
                    A rung sits on the losing side of the stop
                    {side === 'BUY'
                      ? ' — for a long every entry has to be above it.'
                      : ' — for a short every entry has to be below it.'}{' '}
                    Nothing is sized until that is fixed.
                  </p>
                )}

                {overAllocated && (
                  <p className="mt-0.5 text-[10px] text-loss">
                    The rungs add up to {plan.allocatedPercent}% of a position
                    you only buy 100% of.
                  </p>
                )}

                {underAllocated && (
                  <p className="mt-0.5 text-[10px] text-obsidian-muted">
                    The rungs add up to {plan.allocatedPercent}% — the ladder is
                    sized as though that is the whole position.
                  </p>
                )}

                {plan.hasEmptyLeg && (
                  <p className="mt-0.5 text-[10px] text-loss">
                    A rung this small buys no whole shares. Scaling in needs a
                    budget big enough to divide.
                  </p>
                )}

                {plan.accountFraction !== null && plan.accountFraction > 1 && (
                  <p className="mt-0.5 text-[10px] text-loss">
                    Costs more than the account holds — needs margin.
                  </p>
                )}

                {/* Nothing above has touched the form. This is the one step
                    that does, and it fills both fields together: a blended
                    entry without its share count is not a plan, it is half of
                    one. */}
                {onApply !== undefined &&
                  plan.totalShares !== null &&
                  plan.totalShares > 0 && (
                    <button
                      type="button"
                      onClick={() =>
                        onApply(
                          formatPrice(plan.blend!.entry),
                          plan.totalShares as number
                        )
                      }
                      disabled={disabled}
                      className="mt-2 w-full rounded-lg border border-obsidian-border bg-obsidian-bg px-3 py-1.5 text-[11px] text-slate-300 transition-colors hover:border-slate-600 hover:text-slate-100 disabled:opacity-50"
                    >
                      Use {formatPrice(plan.blend.entry)} × {plan.totalShares}{' '}
                      share{plan.totalShares === 1 ? '' : 's'}
                    </button>
                  )}
              </div>
            )
          )}
        </>
      )}
    </div>
  );
};

export default ScaledEntryPlanner;

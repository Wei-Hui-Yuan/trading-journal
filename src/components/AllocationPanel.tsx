'use client';

import React, { useMemo } from 'react';
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts';
import { PiggyBank, Target } from 'lucide-react';

import type { Holding } from '@/types/investments';

const money = (value: number) =>
  value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** Stable across renders as long as the ticker set does not change -- every
 * ring and every list row for a given ticker gets the same color, which is
 * the only thing that makes an "actual vs target" comparison readable. */
const PALETTE = [
  '#38bdf8', '#34d399', '#fbbf24', '#f472b6', '#a78bfa', '#fb923c',
  '#22d3ee', '#facc15', '#4ade80', '#f87171', '#818cf8', '#2dd4bf',
];

interface Slice {
  name: string;
  value: number;
  ring: 'Actual' | 'Target';
  pct: number;
}

const CustomTooltip: React.FC<{ active?: boolean; payload?: { payload: Slice }[] }> = ({
  active,
  payload,
}) => {
  if (!active || !payload?.length) return null;
  const s = payload[0].payload;
  return (
    <div className="rounded-lg border border-obsidian-border bg-obsidian-card px-2.5 py-1.5 text-[11px] shadow-xl">
      <div className="font-semibold text-slate-100">{s.name}</div>
      <div className="text-obsidian-muted">
        {s.ring}: {money(s.value)} ({s.pct.toFixed(1)}%)
      </div>
    </div>
  );
};

/**
 * Deployment discipline, not performance. Both rings are cost basis or a
 * planned dollar figure -- never market value -- because this panel answers
 * "did I put in what I meant to", and a name that is simply up a lot would
 * otherwise read as "over-allocated" for no reason connected to any decision
 * the user made.
 *
 * The two rings are each normalized to their OWN 100%, not a shared
 * denominator. A holding with no target contributes to the actual ring (it
 * is real money) but does not exist in the target ring at all -- there is
 * nothing dishonest about that split, but it does mean the two rings are
 * answering "how IS my money split" and "how did I PLAN to split it",
 * not two slices of one pie.
 */
export const AllocationPanel: React.FC<{
  holdings: Holding[];
  onSetTarget: (ticker: string) => void;
}> = ({ holdings, onSetTarget }) => {
  const {
    actualSlices,
    targetSlices,
    colorOf,
    gaps,
    dca,
    unplanned,
    totalDeployed,
    totalTarget,
  } = useMemo(() => {
    const funded = holdings.filter((h) => h.cost_basis > 0);
    const totalDeployed = funded.reduce((sum, h) => sum + h.cost_basis, 0);

    const targeted = holdings.filter((h) => (h.planned_allocation ?? 0) > 0);
    const totalTarget = targeted.reduce((sum, h) => sum + (h.planned_allocation ?? 0), 0);

    const allTickers = Array.from(
      new Set([...funded.map((h) => h.ticker), ...targeted.map((h) => h.ticker)])
    ).sort();
    const colorOf = new Map(allTickers.map((t, i) => [t, PALETTE[i % PALETTE.length]]));

    const actualSlices: Slice[] = funded
      .map((h) => ({
        name: h.ticker,
        value: h.cost_basis,
        ring: 'Actual' as const,
        pct: totalDeployed > 0 ? (h.cost_basis / totalDeployed) * 100 : 0,
      }))
      .sort((a, b) => b.value - a.value);

    const targetSlices: Slice[] = targeted
      .map((h) => ({
        name: h.ticker,
        value: h.planned_allocation as number,
        ring: 'Target' as const,
        pct: totalTarget > 0 ? ((h.planned_allocation as number) / totalTarget) * 100 : 0,
      }))
      .sort((a, b) => b.value - a.value);

    // Sorted worst-funded first -- the largest dollar shortfall against its
    // own target is the one a DCA deposit should close first.
    const gaps = targeted
      .map((h) => {
        const target = h.planned_allocation as number;
        return {
          ticker: h.ticker,
          target,
          deployed: h.cost_basis,
          gap: target - h.cost_basis,
          fundedPct: (h.cost_basis / target) * 100,
        };
      })
      .sort((a, b) => b.gap - a.gap);

    const dca = gaps.length && gaps[0].gap > 0 ? gaps[0] : null;

    // Real capital, no plan attached. Distinct from a holding with $0 cost
    // basis and no target (nothing to show for either) -- these have money
    // in them today and simply never got a target, most often because they
    // arrived through a sync rather than being added by hand.
    const unplanned = funded
      .filter((h) => !(h.planned_allocation && h.planned_allocation > 0))
      .sort((a, b) => b.cost_basis - a.cost_basis);

    return { actualSlices, targetSlices, colorOf, gaps, dca, unplanned, totalDeployed, totalTarget };
  }, [holdings]);

  if (totalDeployed === 0 && totalTarget === 0) return null;

  return (
    <div className="grid grid-cols-1 gap-3 rounded-xl border border-obsidian-border bg-obsidian-card p-4 lg:grid-cols-[minmax(0,280px)_1fr]">
      {/* ---------------- donut ---------------- */}
      <div>
        <div className="text-[10px] font-semibold uppercase tracking-wider text-slate-300">
          Actual vs. target allocation
        </div>
        <div className="text-[10px] text-obsidian-muted">
          Outer ring: actual (cost basis) · Inner ring: target (planned $)
        </div>
        <div className="h-[220px]">
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie
                data={actualSlices}
                dataKey="value"
                nameKey="name"
                cx="50%"
                cy="50%"
                innerRadius={68}
                outerRadius={95}
                stroke="var(--color-obsidian-card, #0f172a)"
                strokeWidth={1}
              >
                {actualSlices.map((s) => (
                  <Cell key={s.name} fill={colorOf.get(s.name)} />
                ))}
              </Pie>
              <Pie
                data={targetSlices}
                dataKey="value"
                nameKey="name"
                cx="50%"
                cy="50%"
                innerRadius={34}
                outerRadius={62}
                stroke="var(--color-obsidian-card, #0f172a)"
                strokeWidth={1}
              >
                {targetSlices.map((s) => (
                  <Cell key={s.name} fill={colorOf.get(s.name)} fillOpacity={0.55} />
                ))}
              </Pie>
              <Tooltip content={<CustomTooltip />} />
            </PieChart>
          </ResponsiveContainer>
        </div>
        {targetSlices.length === 0 && (
          <p className="text-[10px] text-obsidian-muted">
            No targets set yet — the inner ring fills in as you set one per holding.
          </p>
        )}

        {/* Hand-rolled legend -- Recharts' own Legend does not know that two
            Pies sharing a color means "same ticker, two rings". */}
        <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1">
          {Array.from(colorOf.entries()).map(([ticker, color]) => (
            <div key={ticker} className="flex items-center gap-1 text-[10px] text-slate-300">
              <span
                className="h-2 w-2 shrink-0 rounded-full"
                style={{ backgroundColor: color }}
              />
              {ticker}
            </div>
          ))}
        </div>
      </div>

      {/* ---------------- gaps + DCA + unplanned ---------------- */}
      <div className="space-y-3">
        {dca && (
          <div className="flex items-start gap-2 rounded-lg border border-sky-500/25 bg-sky-500/5 px-3 py-2.5">
            <PiggyBank className="mt-0.5 h-4 w-4 shrink-0 text-sky-400" />
            <div className="text-[11px] leading-relaxed text-sky-100">
              <span className="font-semibold">
                {dca.ticker} is furthest from its target
              </span>{' '}
              — {money(dca.deployed)} of {money(dca.target)} deployed (
              {dca.fundedPct.toFixed(0)}%). Next DCA suggestion:{' '}
              <span className="font-semibold">{money(dca.gap)} into {dca.ticker}</span>.
            </div>
          </div>
        )}

        {gaps.length > 0 && (
          <div>
            <div className="mb-1 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-slate-300">
              <Target className="h-3 w-3" />
              Against target, most under-funded first
            </div>
            <div className="space-y-1">
              {gaps.map((g) => (
                <div
                  key={g.ticker}
                  className="flex items-center justify-between gap-2 rounded-lg px-2 py-1.5 text-[11px] hover:bg-slate-700/25"
                >
                  <div className="flex items-center gap-1.5">
                    <span
                      className="h-2 w-2 shrink-0 rounded-full"
                      style={{ backgroundColor: colorOf.get(g.ticker) }}
                    />
                    <span className="font-medium text-slate-200">{g.ticker}</span>
                  </div>
                  <div className="font-mono text-obsidian-muted">
                    {money(g.deployed)} / {money(g.target)}
                  </div>
                  <div
                    className={`w-24 shrink-0 text-right font-mono ${
                      g.gap > 0 ? 'text-amber-300' : 'text-win'
                    }`}
                  >
                    {g.gap > 0 ? `${money(g.gap)} short` : 'funded'}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {unplanned.length > 0 && (
          <div>
            <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-slate-300">
              No target set
            </div>
            <div className="space-y-1">
              {unplanned.map((h) => (
                <div
                  key={h.ticker}
                  className="flex items-center justify-between gap-2 rounded-lg px-2 py-1.5 text-[11px]"
                >
                  <div className="flex items-center gap-1.5">
                    <span
                      className="h-2 w-2 shrink-0 rounded-full"
                      style={{ backgroundColor: colorOf.get(h.ticker) }}
                    />
                    <span className="font-medium text-slate-200">{h.ticker}</span>
                    <span className="text-obsidian-muted">{money(h.cost_basis)} invested</span>
                  </div>
                  <button
                    type="button"
                    onClick={() => onSetTarget(h.ticker)}
                    className="shrink-0 text-[10px] text-slate-500 underline decoration-dotted transition-colors hover:text-slate-300"
                  >
                    Set a target →
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default AllocationPanel;

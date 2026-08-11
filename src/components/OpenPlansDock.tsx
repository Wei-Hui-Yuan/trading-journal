'use client';

import React, { useState } from 'react';
import {
  AlertCircle,
  ChevronRight,
  ClipboardList,
  Link2,
  Loader2,
  Trash2,
} from 'lucide-react';

import { useAttachPlan, useCancelPlan, usePlans } from '@/hooks/useTradeInbox';
import { PlanChartView } from '@/components/PlanChart';
import { PlanModal } from '@/components/PlanModal';
import type { PlanCandidate, TradePlan } from '@/types/api';

const when = new Intl.DateTimeFormat('en-US', {
  month: 'short',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
  timeZone: 'America/New_York',
});

/** A price, at the precision the instrument warrants. */
const price = (n: number | null) =>
  n === null ? '—' : n < 1 ? n.toFixed(4) : n.toFixed(2);

const money = (n: number) =>
  n.toLocaleString('en-US', { style: 'currency', currency: 'USD' });

/** Trailing zeros on a fractional size are noise; 0.25 should read as 0.25. */
const qty = (n: number | null) =>
  n === null ? '—' : Number(n.toFixed(8)).toString();

/**
 * "Filled 10:12 ET — 1 min before this plan was saved."
 *
 * Puts the fill's own timestamp next to the plan's, in the same timezone the
 * rest of the row already renders in — the two times sitting side by side is
 * what turns "why won't this attach" into "oh, it's a minute late", instead
 * of a gap the trader has to reconstruct by hand across two different clocks.
 */
function candidateContext(candidate: PlanCandidate): string {
  const at = `${when.format(new Date(candidate.entry_date))} ET`;
  const minutes = candidate.minutes_from_fill_to_plan;
  if (minutes === null) return `Filled ${at}`;
  const abs = Math.abs(minutes);
  const span = abs < 1 ? 'under a minute' : `${Math.round(abs)} min`;
  return `Filled ${at} — ${span} ${minutes >= 0 ? 'before' : 'after'} this plan was saved`;
}

/**
 * What the plan pays if the target is reached, in dollars.
 *
 * R is size-independent, which is what makes it comparable across trades — but
 * it is also why "1.22R" alone does not tell you whether this setup is worth
 * the screen time. 1.22R on $22 risk and 1.22R on $2,200 are the same number
 * and very different decisions.
 *
 * Computed from the prices and size rather than as planned_r x risk_amount:
 * planned_r is stored rounded to two decimals, so multiplying it back out
 * drifts. Here 1.216... x 22.20 would show $27.08 where the trade actually
 * pays $27.00.
 *
 * Direction-aware, since a short profits when the target sits BELOW the entry.
 */
function plannedReward(plan: TradePlan): number | null {
  const { planned_entry: entry, take_profit: target, quantity, direction } = plan;
  if (entry === null || target === null || quantity === null) return null;
  const perShare = direction === 'BUY' ? target - entry : entry - target;
  return perShare * quantity;
}

/**
 * What the plan loses if the stop is hit, in dollars.
 *
 * Derived from the plan's own entry, stop and quantity rather than read from
 * `risk_amount`. That column is a snapshot taken when the plan was sized, and
 * editing the quantity or a price afterwards used to leave it behind — a plan
 * showing "Risk $20.00" while its own numbers risked $5.00. Deriving it here
 * means the figure can never contradict the three prices printed beside it.
 *
 * Direction-aware, and null when the stop sits on the wrong side of the entry:
 * a long stopped above its entry has no risk to state, and a negative one
 * would read as a guaranteed profit.
 */
function plannedRisk(plan: TradePlan): number | null {
  const { planned_entry: entry, stop_loss: stop, quantity, direction } = plan;
  if (entry === null || stop === null || quantity === null) return null;
  const perShare = direction === 'BUY' ? entry - stop : stop - entry;
  return perShare > 0 ? perShare * quantity : null;
}

/**
 * Trades you have committed to but not yet entered.
 *
 * These deliberately live outside `trades`, so nothing here is counted in P&L,
 * win rate or exposure — a plan is an intention, and treating it as a position
 * is precisely the bug this replaced. The dock exists because that invisibility
 * cuts both ways: a plan you cannot see is a plan you forget you wrote, and a
 * forgotten plan eventually attaches itself to an unrelated fill.
 *
 * Collapsed to nothing when there are no open plans. An empty panel on the
 * dashboard every day would train you to stop looking at this one.
 *
 * Editing opens the same modal used to create a plan (`PlanModal`, seeded from
 * the row) rather than an inline form -- the inline version could only touch
 * four price fields, leaving ticker, direction, strategy and thesis
 * uneditable once a plan was saved.
 *
 * A plan the sync could not auto-attach is not always still waiting -- the
 * fill may already be sitting in the ledger, orphaned because it arrived a
 * little outside `_plan_can_claim`'s window. `plan.candidates` is exactly
 * that: unlinked fills the backend already knows could be this plan's, so the
 * dock can offer the same manual attach the Journal exposes on a round trip,
 * without the trader having to go find it there first.
 */
export const OpenPlansDock: React.FC = () => {
  const { data: plans, isPending, isError, error } = usePlans('OPEN');
  const cancelMutation = useCancelPlan();
  const attachMutation = useAttachPlan();

  const [editingPlan, setEditingPlan] = useState<TradePlan | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  if (isPending) return null;

  if (isError) {
    return (
      <section className="mb-6 rounded-xl border border-obsidian-border bg-obsidian-card p-4">
        <div className="flex items-start text-xs text-loss">
          <AlertCircle className="mr-1.5 h-4 w-4 shrink-0" />
          <span>{error?.message ?? 'Could not load trade plans.'}</span>
        </div>
      </section>
    );
  }

  const rows = plans ?? [];
  if (rows.length === 0) return null;

  return (
    <section className="mb-6 rounded-xl border border-amber-500/25 bg-obsidian-card">
      <div className="flex items-center justify-between border-b border-obsidian-border px-4 py-3">
        <div className="flex items-center gap-2">
          <ClipboardList className="h-4 w-4 text-amber-400" />
          <h2 className="text-sm font-semibold tracking-wide text-slate-200">
            OPEN TRADE PLANS
          </h2>
          <span className="rounded bg-amber-500/15 px-1.5 py-0.5 font-mono text-[10px] text-amber-300">
            {rows.length}
          </span>
        </div>
        <span className="text-[10px] text-obsidian-muted">
          Not in the ledger — nothing here affects your stats
        </span>
      </div>

      <div className="divide-y divide-obsidian-border/60">
        {rows.map((plan) => {
          const reward = plannedReward(plan);
          const risk = plannedRisk(plan);
          const busy = cancelMutation.isPending && cancelMutation.variables === plan.id;

          return (
            <div key={plan.id} className="px-4 py-3">
              <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                <span className="font-semibold text-slate-100">{plan.ticker}</span>
                <span
                  className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${
                    plan.direction === 'BUY'
                      ? 'bg-win/10 text-win'
                      : 'bg-loss/10 text-loss'
                  }`}
                >
                  {plan.direction === 'BUY' ? 'LONG' : 'SHORT'}
                </span>

                {plan.candidates.length === 0 ? (
                  <span className="inline-flex items-center gap-1 rounded bg-amber-500/10 px-2 py-0.5 text-[10px] text-amber-300">
                    <span className="h-1.5 w-1.5 rounded-full bg-amber-400" />
                    Pending · awaiting IBKR sync
                  </span>
                ) : (
                  // Sky, not amber: an amber badge reads as "still waiting",
                  // and this plan is not -- a fill it could belong to already
                  // exists, and the only thing left is a click.
                  <span className="inline-flex items-center gap-1 rounded bg-sky-500/10 px-2 py-0.5 text-[10px] text-sky-300">
                    <Link2 className="h-3 w-3" />
                    {plan.candidates.length === 1
                      ? 'Unlinked fill found'
                      : `${plan.candidates.length} unlinked fills`}
                  </span>
                )}

                {plan.planned_r !== null && (
                  <span className="font-mono text-[11px] text-slate-300">
                    {plan.planned_r.toFixed(2)}R planned
                    {/* The same ratio means very different things at different
                        sizes, so the money sits next to it rather than being
                        left as arithmetic to do in your head.

                        Both sides of it. R is a ratio, so a good one can still
                        be attached to a loss you would not accept — and the
                        number that decides whether to take the trade is what
                        the stop costs, not what the target pays. */}
                    {reward !== null && (
                      <span className="ml-1.5 text-win">+{money(reward)}</span>
                    )}
                    {risk !== null && (
                      <span
                        className="ml-1.5 text-loss"
                        title="What this plan loses if the hard stop is hit, at the quantity planned."
                      >
                        -{money(risk)} at stop
                      </span>
                    )}
                  </span>
                )}

                {plan.created_at && (
                  <span className="ml-auto text-[10px] text-obsidian-muted">
                    {when.format(new Date(plan.created_at))} ET
                  </span>
                )}
              </div>

              {plan.candidates.length > 0 && (
                <div className="mt-2 space-y-1.5 rounded-lg border border-dashed border-sky-500/25 bg-sky-500/5 px-3 py-2">
                  {plan.candidates.map((candidate) => {
                    const attaching =
                      attachMutation.isPending &&
                      attachMutation.variables?.planId === plan.id &&
                      attachMutation.variables?.tradeId === candidate.trade_id;

                    return (
                      <div
                        key={candidate.trade_id}
                        className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]"
                      >
                        <span className="text-sky-100">{candidateContext(candidate)}</span>
                        <span className="font-mono text-[10px] text-obsidian-muted">
                          {qty(candidate.quantity)} @ {price(candidate.avg_entry)}
                          {candidate.fill_count > 1 ? ` · ${candidate.fill_count} fills` : ''}
                        </span>
                        <button
                          type="button"
                          onClick={() => {
                            setFailed(null);
                            attachMutation.mutate(
                              { tradeId: candidate.trade_id, planId: plan.id },
                              { onError: (err) => setFailed(err.message) }
                            );
                          }}
                          disabled={attaching}
                          className="ml-auto inline-flex items-center gap-1 rounded border border-sky-500/30 px-2 py-0.5 text-[10px] text-sky-300 transition-colors hover:border-sky-500/60 hover:bg-sky-500/10 disabled:opacity-50"
                        >
                          {attaching ? (
                            <Loader2 className="h-3 w-3 animate-spin" />
                          ) : (
                            <Link2 className="h-3 w-3" />
                          )}
                          Link to this fill
                        </button>
                      </div>
                    );
                  })}
                </div>
              )}

              <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 font-mono text-[11px] text-obsidian-muted">
                <span>
                  Qty <span className="text-slate-300">{qty(plan.quantity)}</span>
                </span>
                <span>
                  Entry{' '}
                  <span className="text-slate-300">{price(plan.planned_entry)}</span>
                </span>
                <span>
                  Stop <span className="text-loss">{price(plan.stop_loss)}</span>
                </span>
                <span>
                  Target <span className="text-win">{price(plan.take_profit)}</span>
                </span>
                {/* Derived, not `plan.risk_amount`. Showing a stored figure
                    next to the entry, stop and quantity it is supposed to
                    come from invites exactly the contradiction it produced:
                    "Risk $20.00" beside 1 share with a $5 stop distance. */}
                {risk !== null && (
                  <span>
                    Risk <span className="text-slate-300">{money(risk)}</span>
                  </span>
                )}
              </div>

              {/* The thesis and the chart it was written from, side by side.
                  A thumbnail rather than the full capture: the dock is a
                  scannable list, and clicking opens it full size. */}
              {(plan.thesis || plan.has_chart) && (
                <div className="mt-2 flex items-start gap-3">
                  {plan.has_chart && (
                    <PlanChartView planId={plan.id} thumbnail />
                  )}
                  {plan.thesis && (
                    <p className="line-clamp-3 text-[11px] leading-relaxed text-obsidian-muted">
                      {plan.thesis}
                    </p>
                  )}
                </div>
              )}

              <div className="mt-2.5 flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setEditingPlan(plan)}
                  className="inline-flex items-center gap-1 rounded border border-obsidian-border px-2.5 py-1 text-[10px] text-obsidian-muted transition-colors hover:border-slate-600 hover:text-slate-200"
                >
                  <ChevronRight className="h-3 w-3" />
                  Edit
                </button>
                {/* Cancelled, not deleted: a setup you talked yourself out
                    of is evidence about your process, and a row that
                    vanishes takes that with it. */}
                <button
                  type="button"
                  onClick={() => {
                    setFailed(null);
                    cancelMutation.mutate(plan.id, {
                      onError: (err) => setFailed(err.message),
                    });
                  }}
                  disabled={busy}
                  title="Keeps the plan on record as cancelled, and stops it claiming a fill"
                  className="inline-flex items-center gap-1 rounded border border-obsidian-border px-2.5 py-1 text-[10px] text-obsidian-muted transition-colors hover:border-loss/40 hover:text-loss disabled:opacity-50"
                >
                  {busy ? (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  ) : (
                    <Trash2 className="h-3 w-3" />
                  )}
                  Cancel plan
                </button>
              </div>
            </div>
          );
        })}
      </div>

      {failed && (
        <div className="flex items-start border-t border-obsidian-border px-4 py-2.5 text-xs text-loss">
          <AlertCircle className="mr-1.5 mt-px h-3.5 w-3.5 shrink-0" />
          <span>{failed}</span>
        </div>
      )}

      <PlanModal
        open={editingPlan !== null}
        plan={editingPlan}
        onClose={() => setEditingPlan(null)}
      />
    </section>
  );
};

export default OpenPlansDock;

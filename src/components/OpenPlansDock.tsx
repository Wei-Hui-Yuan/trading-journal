'use client';

import React, { useState } from 'react';
import {
  AlertCircle,
  ChevronRight,
  ClipboardList,
  Loader2,
  Trash2,
} from 'lucide-react';

import { useCancelPlan, usePlans, useUpdatePlan } from '@/hooks/useTradeInbox';
import type { TradePlan } from '@/types/api';

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

interface EditDraft {
  planned_entry: string;
  stop_loss: string;
  take_profit: string;
  quantity: string;
}

const draftFrom = (plan: TradePlan): EditDraft => ({
  planned_entry: plan.planned_entry === null ? '' : String(plan.planned_entry),
  stop_loss: plan.stop_loss === null ? '' : String(plan.stop_loss),
  take_profit: plan.take_profit === null ? '' : String(plan.take_profit),
  quantity: plan.quantity === null ? '' : String(plan.quantity),
});

const toNullableNumber = (raw: string): number | null => {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
};

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
 */
export const OpenPlansDock: React.FC = () => {
  const { data: plans, isPending, isError, error } = usePlans('OPEN');
  const cancelMutation = useCancelPlan();
  const updateMutation = useUpdatePlan();

  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<EditDraft | null>(null);
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

  const startEdit = (plan: TradePlan) => {
    setEditing(plan.id);
    setDraft(draftFrom(plan));
    setFailed(null);
  };

  const saveEdit = (plan: TradePlan) => {
    if (!draft) return;
    updateMutation.mutate(
      {
        planId: plan.id,
        payload: {
          planned_entry: toNullableNumber(draft.planned_entry),
          stop_loss: toNullableNumber(draft.stop_loss),
          take_profit: toNullableNumber(draft.take_profit),
          quantity: toNullableNumber(draft.quantity),
        },
      },
      {
        onSuccess: () => {
          setEditing(null);
          setDraft(null);
        },
        onError: (err) => setFailed(err.message),
      }
    );
  };

  const fieldClass =
    'w-full rounded border border-obsidian-border bg-obsidian-bg px-2 py-1 font-mono text-[11px] ' +
    'text-slate-200 focus:border-slate-600 focus:outline-none';

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
          const isEditing = editing === plan.id;
          const busy =
            (cancelMutation.isPending && cancelMutation.variables === plan.id) ||
            (updateMutation.isPending && updateMutation.variables?.planId === plan.id);

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

                <span className="inline-flex items-center gap-1 rounded bg-amber-500/10 px-2 py-0.5 text-[10px] text-amber-300">
                  <span className="h-1.5 w-1.5 rounded-full bg-amber-400" />
                  Pending · awaiting IBKR sync
                </span>

                {plan.planned_r !== null && (
                  <span className="font-mono text-[11px] text-slate-300">
                    {plan.planned_r.toFixed(2)}R planned
                    {/* The same ratio means very different things at different
                        sizes, so the money sits next to it rather than being
                        left as arithmetic to do in your head. */}
                    {(() => {
                      const reward = plannedReward(plan);
                      return reward === null ? null : (
                        <span className="ml-1.5 text-win">+{money(reward)}</span>
                      );
                    })()}
                  </span>
                )}

                {plan.created_at && (
                  <span className="ml-auto text-[10px] text-obsidian-muted">
                    {when.format(new Date(plan.created_at))} ET
                  </span>
                )}
              </div>

              {isEditing && draft ? (
                <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
                  {(
                    [
                      ['quantity', 'Qty'],
                      ['planned_entry', 'Entry'],
                      ['stop_loss', 'Stop'],
                      ['take_profit', 'Target'],
                    ] as const
                  ).map(([key, label]) => (
                    <label key={key} className="block">
                      <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
                        {label}
                      </span>
                      <input
                        type="number"
                        step="0.0001"
                        min="0"
                        value={draft[key]}
                        onChange={(e) =>
                          setDraft({ ...draft, [key]: e.target.value })
                        }
                        className={`mt-1 ${fieldClass}`}
                      />
                    </label>
                  ))}
                </div>
              ) : (
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
                  {plan.risk_amount !== null && (
                    <span>
                      Risk{' '}
                      <span className="text-slate-300">{money(plan.risk_amount)}</span>
                    </span>
                  )}
                </div>
              )}

              {plan.thesis && !isEditing && (
                <p className="mt-2 line-clamp-2 text-[11px] leading-relaxed text-obsidian-muted">
                  {plan.thesis}
                </p>
              )}

              <div className="mt-2.5 flex items-center gap-2">
                {isEditing ? (
                  <>
                    <button
                      type="button"
                      onClick={() => saveEdit(plan)}
                      disabled={busy}
                      className="inline-flex items-center gap-1 rounded border border-win-border bg-win-glow px-2.5 py-1 text-[10px] font-medium text-win transition-colors hover:bg-win/20 disabled:opacity-50"
                    >
                      {busy && <Loader2 className="h-3 w-3 animate-spin" />}
                      Save
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setEditing(null);
                        setDraft(null);
                        setFailed(null);
                      }}
                      className="rounded border border-obsidian-border px-2.5 py-1 text-[10px] text-obsidian-muted transition-colors hover:text-slate-200"
                    >
                      Cancel edit
                    </button>
                  </>
                ) : (
                  <>
                    <button
                      type="button"
                      onClick={() => startEdit(plan)}
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
                  </>
                )}
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
    </section>
  );
};

export default OpenPlansDock;

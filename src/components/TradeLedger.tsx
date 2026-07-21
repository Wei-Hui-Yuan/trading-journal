'use client';

import React, { useMemo, useState } from 'react';
import {
  AlertCircle,
  ArrowDownRight,
  ArrowUpRight,
  ChevronRight,
  Loader2,
  NotebookPen,
  Search,
  Trash2,
} from 'lucide-react';

import {
  useAnnotateTrade,
  useDeleteTrade,
  useReviewPosition,
  useRoundTrips,
  useStrategies,
} from '@/hooks/useTradeInbox';
import type { RoundTrip } from '@/types/api';

const dateFormatter = new Intl.DateTimeFormat('en-US', {
  year: 'numeric',
  month: 'short',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
  timeZone: 'America/New_York',
});

/** Trailing zeros on a fractional size are noise; 0.25 should read as 0.25. */
function formatQuantity(quantity: number): string {
  return Number(quantity.toFixed(8)).toString();
}

/** Empty means "not recorded" and must reach the API as null, never as 0. */
function parseNumber(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

function toField(value: number | null | undefined): string {
  return value === null || value === undefined ? '' : String(value);
}

const EXIT_REASONS = [
  'Target hit',
  'Stopped out',
  'Trailing stop',
  'Manual exit',
  'Time stop',
  'Thesis invalidated',
];

type Filter = 'all' | 'open' | 'closed';

interface PlanDraft {
  strategyId: string;
  thesis: string;
  plannedEntry: string;
  stopLoss: string;
  actualStopLoss: string;
  target: string;
  riskPercent: string;
  riskAmount: string;
  conviction: string;
  emotionalState: string;
}

interface ReviewDraft {
  exitReason: string;
  wentWell: string;
  wentWrong: string;
  lessons: string;
  grade: string;
  idealEntry: string;
  idealStop: string;
  idealTarget: string;
  revisedEntry: string;
  revisedStop: string;
  revisedTarget: string;
}

function planDraftFrom(rt: RoundTrip): PlanDraft {
  return {
    strategyId: rt.strategy_id ?? '',
    thesis: rt.thesis ?? '',
    plannedEntry: toField(rt.planned_entry),
    stopLoss: toField(rt.stop_loss),
    actualStopLoss: toField(rt.actual_stop_loss),
    target: toField(rt.target),
    riskPercent: toField(rt.risk_percent),
    riskAmount: toField(rt.risk_amount),
    conviction: toField(rt.conviction),
    emotionalState: rt.emotional_state ?? '',
  };
}

function reviewDraftFrom(rt: RoundTrip): ReviewDraft {
  return {
    exitReason: rt.exit_reason ?? '',
    wentWell: rt.review_went_well ?? '',
    wentWrong: rt.review_went_wrong ?? '',
    lessons: rt.review_lessons ?? '',
    grade: rt.trade_grade ?? '',
    idealEntry: toField(rt.ideal_entry),
    idealStop: toField(rt.ideal_stop),
    idealTarget: toField(rt.ideal_target),
    revisedEntry: toField(rt.revised_entry),
    revisedStop: toField(rt.revised_stop),
    revisedTarget: toField(rt.revised_target),
  };
}

const fieldClass =
  'w-full rounded-lg bg-obsidian-bg border border-obsidian-border px-3 py-2 text-xs text-slate-200 ' +
  'placeholder:text-obsidian-muted focus:outline-none focus:border-slate-600 transition-colors';

const labelClass = 'text-[10px] uppercase tracking-wide text-obsidian-muted';

const Field: React.FC<{
  label: string;
  hint?: string;
  children: React.ReactNode;
}> = ({ label, hint, children }) => (
  <label className="block">
    <span className={labelClass}>{label}</span>
    {children}
    {hint && <span className="mt-1 block text-[10px] text-slate-600">{hint}</span>}
  </label>
);

const SectionHeading: React.FC<{ title: string; blurb: string }> = ({ title, blurb }) => (
  <div className="mb-3">
    <h4 className="text-[11px] font-semibold uppercase tracking-wider text-slate-300">
      {title}
    </h4>
    <p className="text-[10px] text-obsidian-muted">{blurb}</p>
  </div>
);

/** R is the headline number, so it gets colour and a sign. */
const RBadge: React.FC<{ value: number | null; label?: string }> = ({ value, label }) => {
  if (value === null || value === undefined) return null;
  const positive = value >= 0;
  return (
    <span
      className={`shrink-0 rounded px-1.5 py-0.5 font-mono text-[10px] ${
        positive ? 'bg-win/10 text-win' : 'bg-loss/10 text-loss'
      }`}
      title={label ?? 'Realised R — reward in units of the risk taken'}
    >
      {positive ? '+' : ''}
      {value.toFixed(2)}R
    </span>
  );
};

/**
 * The journal, grouped by trade idea rather than by execution.
 *
 * A single CRWD trade used to render as four rows, because the broker filled
 * the entry with two orders and the exit with two more. Those executions were
 * always one round trip in the data (`positions` + `position_fills`); this view
 * finally uses it. Open exposure — which has no position row at all, and so was
 * invisible on every other surface — is reconstructed from unmatched fills.
 *
 * The plan is saved onto the round trip's *opening* execution and the review
 * onto its position. They are separate endpoints, so they get separate Save
 * buttons: one control writing to two resources cannot report a partial failure
 * honestly.
 */
export const TradeLedger: React.FC = () => {
  const { data: roundTrips, isLoading, error } = useRoundTrips();
  const { data: strategies } = useStrategies();
  const annotate = useAnnotateTrade();
  const review = useReviewPosition();
  const deleteTradeMutation = useDeleteTrade();

  const [expanded, setExpanded] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>('all');
  const [query, setQuery] = useState('');
  const [planDrafts, setPlanDrafts] = useState<Record<string, PlanDraft>>({});
  const [reviewDrafts, setReviewDrafts] = useState<Record<string, ReviewDraft>>({});

  const visible = useMemo(() => {
    const term = query.trim().toUpperCase();
    return (roundTrips ?? []).filter((rt) => {
      if (filter === 'open' && rt.kind !== 'open') return false;
      if (filter === 'closed' && rt.kind !== 'closed') return false;
      return !term || rt.symbol.includes(term);
    });
  }, [roundTrips, filter, query]);

  const openCount = (roundTrips ?? []).filter((rt) => rt.kind === 'open').length;

  const planOf = (rt: RoundTrip) => planDrafts[rt.key] ?? planDraftFrom(rt);
  const reviewOf = (rt: RoundTrip) => reviewDrafts[rt.key] ?? reviewDraftFrom(rt);

  const setPlan = (rt: RoundTrip, patch: Partial<PlanDraft>) =>
    setPlanDrafts((prev) => ({ ...prev, [rt.key]: { ...planOf(rt), ...patch } }));
  const setReview = (rt: RoundTrip, patch: Partial<ReviewDraft>) =>
    setReviewDrafts((prev) => ({ ...prev, [rt.key]: { ...reviewOf(rt), ...patch } }));

  const savePlan = (rt: RoundTrip) => {
    if (!rt.plan_trade_id) return;
    const d = planOf(rt);
    annotate.mutate({
      id: rt.plan_trade_id,
      payload: {
        strategy_id: d.strategyId || null,
        thesis: d.thesis.trim() || null,
        planned_entry: parseNumber(d.plannedEntry),
        stop_loss: parseNumber(d.stopLoss),
        actual_stop_loss: parseNumber(d.actualStopLoss),
        target: parseNumber(d.target),
        risk_percent: parseNumber(d.riskPercent),
        risk_amount: parseNumber(d.riskAmount),
        conviction: parseNumber(d.conviction),
        emotional_state: d.emotionalState.trim() || null,
      },
    });
  };

  const saveReview = (rt: RoundTrip) => {
    if (!rt.position_id) return;
    const d = reviewOf(rt);
    review.mutate({
      id: rt.position_id,
      payload: {
        exit_reason: d.exitReason || null,
        review_went_well: d.wentWell.trim() || null,
        review_went_wrong: d.wentWrong.trim() || null,
        review_lessons: d.lessons.trim() || null,
        trade_grade: d.grade || null,
        ideal_entry: parseNumber(d.idealEntry),
        ideal_stop: parseNumber(d.idealStop),
        ideal_target: parseNumber(d.idealTarget),
        revised_entry: parseNumber(d.revisedEntry),
        revised_stop: parseNumber(d.revisedStop),
        revised_target: parseNumber(d.revisedTarget),
      },
    });
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16 text-sm text-obsidian-muted">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        Loading journal…
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center py-16 text-sm text-loss">
        <AlertCircle className="mr-2 h-4 w-4" />
        {(error as Error).message}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Controls */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative min-w-[180px] flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-obsidian-muted" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter by ticker…"
            className={`${fieldClass} pl-8`}
          />
        </div>
        <div className="flex overflow-hidden rounded-lg border border-obsidian-border">
          {(['all', 'open', 'closed'] as const).map((f) => (
            <button
              key={f}
              type="button"
              onClick={() => setFilter(f)}
              className={`px-3 py-2 text-[11px] uppercase tracking-wider transition-colors ${
                filter === f
                  ? 'bg-slate-800 text-slate-100'
                  : 'text-obsidian-muted hover:text-slate-300'
              }`}
            >
              {f}
              {f === 'open' && openCount > 0 && (
                <span className="ml-1.5 font-mono text-amber-300">{openCount}</span>
              )}
            </button>
          ))}
        </div>
      </div>

      {visible.length === 0 ? (
        <p className="py-16 text-center text-sm text-obsidian-muted">
          No trades match this filter.
        </p>
      ) : (
        <div className="space-y-2">
          {visible.map((rt) => {
            const isBuy = rt.direction === 'BUY';
            const isOpen = rt.kind === 'open';
            const plan = planOf(rt);
            const rev = reviewOf(rt);
            const strategyName = rt.strategy_id
              ? (strategies ?? []).find((s) => s.id === rt.strategy_id)?.name ?? null
              : null;

            return (
              <div key={rt.key} className="rounded-xl border border-obsidian-border bg-obsidian-card">
                <button
                  type="button"
                  onClick={() => setExpanded((c) => (c === rt.key ? null : rt.key))}
                  className="flex w-full items-center gap-3 px-4 py-3 text-left"
                >
                  <ChevronRight
                    className={`h-3.5 w-3.5 shrink-0 text-obsidian-muted transition-transform ${
                      expanded === rt.key ? 'rotate-90' : ''
                    }`}
                  />
                  <div
                    className={`rounded-lg p-1.5 ${isBuy ? 'bg-win/10 text-win' : 'bg-loss/10 text-loss'}`}
                  >
                    {isBuy ? (
                      <ArrowUpRight className="h-3.5 w-3.5" />
                    ) : (
                      <ArrowDownRight className="h-3.5 w-3.5" />
                    )}
                  </div>

                  <span className="w-16 shrink-0 font-semibold text-slate-100">{rt.symbol}</span>

                  <span className="w-44 shrink-0 font-mono text-[11px] text-obsidian-muted">
                    {formatQuantity(rt.quantity)} @ {rt.entry_price}
                    {rt.exit_price !== null && ` → ${rt.exit_price}`}
                  </span>

                  <span
                    className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wider ${
                      isOpen ? 'bg-amber-500/10 text-amber-300' : 'bg-slate-800 text-slate-400'
                    }`}
                  >
                    {isOpen ? 'Open' : 'Closed'}
                  </span>

                  {/* The count is what makes grouping legible: "4 fills" is the
                      difference between one trade and four mystery rows. */}
                  {rt.execution_count > 1 && (
                    <span
                      className="shrink-0 rounded bg-slate-800 px-1.5 py-0.5 font-mono text-[10px] text-slate-400"
                      title={`${rt.execution_count} executions in this round trip`}
                    >
                      {rt.execution_count} fills
                    </span>
                  )}

                  <RBadge value={rt.r_multiple} />

                  {rt.realized_pnl !== null && (
                    <span
                      className={`shrink-0 font-mono text-[11px] ${
                        rt.realized_pnl >= 0 ? 'text-win' : 'text-loss'
                      }`}
                    >
                      {rt.realized_pnl >= 0 ? '+' : ''}
                      {rt.realized_pnl.toFixed(2)}
                    </span>
                  )}

                  {strategyName && (
                    <span className="hidden shrink-0 rounded bg-indigo-500/10 px-1.5 py-0.5 text-[10px] text-indigo-300 sm:inline">
                      {strategyName}
                    </span>
                  )}
                  {rt.thesis && (
                    <NotebookPen className="hidden h-3 w-3 shrink-0 text-slate-500 sm:block" />
                  )}

                  <span className="ml-auto shrink-0 font-mono text-[10px] text-obsidian-muted">
                    {dateFormatter.format(new Date(rt.exit_time ?? rt.entry_time))}
                  </span>
                </button>

                {expanded === rt.key && (
                  <div className="space-y-6 border-t border-obsidian-border px-4 py-4">
                    {/* ---------------- THE PLAN ---------------- */}
                    <section>
                      <SectionHeading
                        title="The Plan"
                        blurb="Written at entry. Saved on this round trip's opening execution, so a scale-in has one stop, not several."
                      />

                      <div className="grid gap-3 sm:grid-cols-2">
                        <Field label="Strategy">
                          <select
                            value={plan.strategyId}
                            onChange={(e) => setPlan(rt, { strategyId: e.target.value })}
                            className={`mt-1 ${fieldClass}`}
                          >
                            <option value="">— None —</option>
                            {(strategies ?? []).map((s) => (
                              <option key={s.id} value={s.id}>
                                {s.name}
                              </option>
                            ))}
                          </select>
                        </Field>

                        <Field label="Conviction (1–5)" hint="Rated at entry, before the outcome.">
                          <select
                            value={plan.conviction}
                            onChange={(e) => setPlan(rt, { conviction: e.target.value })}
                            className={`mt-1 ${fieldClass}`}
                          >
                            <option value="">— Unrated —</option>
                            {[1, 2, 3, 4, 5].map((n) => (
                              <option key={n} value={n}>
                                {n}
                              </option>
                            ))}
                          </select>
                        </Field>
                      </div>

                      <div className="mt-3 grid gap-3 sm:grid-cols-4">
                        <Field label="Planned entry">
                          <input
                            type="number"
                            step="any"
                            value={plan.plannedEntry}
                            onChange={(e) => setPlan(rt, { plannedEntry: e.target.value })}
                            className={`mt-1 ${fieldClass}`}
                          />
                        </Field>
                        <Field label="Actual entry" hint="Quantity-weighted across fills.">
                          <input
                            value={rt.entry_price}
                            readOnly
                            className={`mt-1 ${fieldClass} cursor-not-allowed opacity-60`}
                          />
                        </Field>
                        <Field label="Planned stop">
                          <input
                            type="number"
                            step="any"
                            value={plan.stopLoss}
                            onChange={(e) => setPlan(rt, { stopLoss: e.target.value })}
                            className={`mt-1 ${fieldClass}`}
                          />
                        </Field>
                        <Field label="Actual stop" hint="Where it really sat, after moves.">
                          <input
                            type="number"
                            step="any"
                            value={plan.actualStopLoss}
                            onChange={(e) => setPlan(rt, { actualStopLoss: e.target.value })}
                            className={`mt-1 ${fieldClass}`}
                          />
                        </Field>
                      </div>

                      <div className="mt-3 grid gap-3 sm:grid-cols-4">
                        <Field label="Target">
                          <input
                            type="number"
                            step="any"
                            value={plan.target}
                            onChange={(e) => setPlan(rt, { target: e.target.value })}
                            className={`mt-1 ${fieldClass}`}
                          />
                        </Field>
                        <Field label="Risk %">
                          <input
                            type="number"
                            step="any"
                            value={plan.riskPercent}
                            onChange={(e) => setPlan(rt, { riskPercent: e.target.value })}
                            className={`mt-1 ${fieldClass}`}
                          />
                        </Field>
                        <Field label="Risk $" hint="Turns R back into money.">
                          <input
                            type="number"
                            step="any"
                            value={plan.riskAmount}
                            onChange={(e) => setPlan(rt, { riskAmount: e.target.value })}
                            className={`mt-1 ${fieldClass}`}
                          />
                        </Field>
                        <Field label="State of mind">
                          <input
                            value={plan.emotionalState}
                            onChange={(e) => setPlan(rt, { emotionalState: e.target.value })}
                            placeholder="Calm / rushed / revenge…"
                            className={`mt-1 ${fieldClass}`}
                          />
                        </Field>
                      </div>

                      <div className="mt-3">
                        <Field label="Why this trade?">
                          <textarea
                            value={plan.thesis}
                            onChange={(e) => setPlan(rt, { thesis: e.target.value })}
                            rows={3}
                            placeholder="Setup, trigger, and what would prove you wrong."
                            className={`mt-1 resize-y ${fieldClass}`}
                          />
                        </Field>
                      </div>

                      <div className="mt-3 flex items-center justify-between">
                        <div className="flex items-center gap-3 text-[10px] text-obsidian-muted">
                          {rt.planned_r_multiple !== null && (
                            <span>
                              Planned R:R{' '}
                              <span className="font-mono text-slate-300">
                                {rt.planned_r_multiple.toFixed(2)}
                              </span>
                            </span>
                          )}
                          {rt.r_multiple !== null ? (
                            <span>
                              Realised{' '}
                              <span
                                className={`font-mono ${
                                  rt.r_multiple >= 0 ? 'text-win' : 'text-loss'
                                }`}
                              >
                                {rt.r_multiple.toFixed(2)}R
                              </span>
                            </span>
                          ) : (
                            <span>Set a stop to score this trade in R.</span>
                          )}
                        </div>
                        <button
                          type="button"
                          onClick={() => savePlan(rt)}
                          disabled={annotate.isPending}
                          className="rounded-lg border border-win-border bg-win-glow px-3 py-1.5 text-[11px] text-win disabled:opacity-50"
                        >
                          {annotate.isPending ? 'Saving…' : 'Save plan'}
                        </button>
                      </div>
                    </section>

                    {/* ---------------- EXECUTIONS ---------------- */}
                    <section>
                      <SectionHeading
                        title={`Executions (${rt.execution_count})`}
                        blurb="The individual fills behind the weighted averages above."
                      />
                      <div className="overflow-x-auto">
                        <table className="w-full min-w-[360px] text-left text-[11px]">
                          <thead className="text-obsidian-muted">
                            <tr>
                              <th className="pb-1 font-normal">Role</th>
                              <th className="pb-1 font-normal">Qty</th>
                              <th className="pb-1 font-normal">Price</th>
                              <th className="pb-1 font-normal">When</th>
                              <th className="pb-1 text-right font-normal">Action</th>
                            </tr>
                          </thead>
                          <tbody className="font-mono text-slate-300">
                            {rt.fills.map((f) => (
                              <tr key={f.id} className="border-t border-obsidian-border/60">
                                <td className="py-1.5">
                                  <span
                                    className={
                                      f.role === 'OPEN' ? 'text-win' : 'text-loss'
                                    }
                                  >
                                    {f.role}
                                  </span>
                                </td>
                                <td className="py-1.5">{formatQuantity(f.quantity)}</td>
                                <td className="py-1.5">{f.price}</td>
                                <td className="py-1.5 text-obsidian-muted">
                                  {dateFormatter.format(new Date(f.executed_at))}
                                </td>
                                <td className="py-1.5 text-right">
                                  <button
                                    type="button"
                                    onClick={() => {
                                      if (
                                        window.confirm(
                                          'Are you sure you want to delete this trade execution fill?'
                                        )
                                      ) {
                                        deleteTradeMutation.mutate(f.trade_id);
                                      }
                                    }}
                                    disabled={deleteTradeMutation.isPending}
                                    className="inline-flex items-center gap-1 rounded bg-loss/10 px-2 py-0.5 text-[10px] text-loss border border-loss/20 hover:bg-loss/20 transition-colors disabled:opacity-50 font-sans"
                                    title="Delete execution fill"
                                  >
                                    <Trash2 className="h-3 w-3" />
                                    <span>Delete</span>
                                  </button>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </section>

                    {/* ---------------- THE REVIEW ---------------- */}
                    {rt.kind === 'closed' ? (
                      <section>
                        <SectionHeading
                          title="The Review"
                          blurb="Written after the outcome is known. Ideal levels score this trade; revised levels correct the setup for next time."
                        />

                        <div className="grid gap-3 sm:grid-cols-2">
                          <Field label="Exit reason">
                            <select
                              value={rev.exitReason}
                              onChange={(e) => setReview(rt, { exitReason: e.target.value })}
                              className={`mt-1 ${fieldClass}`}
                            >
                              <option value="">— Not set —</option>
                              {EXIT_REASONS.map((r) => (
                                <option key={r} value={r}>
                                  {r}
                                </option>
                              ))}
                            </select>
                          </Field>
                          <Field label="Grade">
                            <select
                              value={rev.grade}
                              onChange={(e) => setReview(rt, { grade: e.target.value })}
                              className={`mt-1 ${fieldClass}`}
                            >
                              <option value="">— Ungraded —</option>
                              {['A', 'B', 'C', 'D', 'F'].map((g) => (
                                <option key={g} value={g}>
                                  {g}
                                </option>
                              ))}
                            </select>
                          </Field>
                        </div>

                        <p className="mt-4 text-[10px] uppercase tracking-wide text-slate-500">
                          With hindsight, this trade&rsquo;s levels should have been
                        </p>
                        <div className="mt-1 grid gap-3 sm:grid-cols-3">
                          <Field label="Ideal entry">
                            <input
                              type="number"
                              step="any"
                              value={rev.idealEntry}
                              onChange={(e) => setReview(rt, { idealEntry: e.target.value })}
                              className={`mt-1 ${fieldClass}`}
                            />
                          </Field>
                          <Field label="Ideal stop">
                            <input
                              type="number"
                              step="any"
                              value={rev.idealStop}
                              onChange={(e) => setReview(rt, { idealStop: e.target.value })}
                              className={`mt-1 ${fieldClass}`}
                            />
                          </Field>
                          <Field label="Ideal target">
                            <input
                              type="number"
                              step="any"
                              value={rev.idealTarget}
                              onChange={(e) => setReview(rt, { idealTarget: e.target.value })}
                              className={`mt-1 ${fieldClass}`}
                            />
                          </Field>
                        </div>

                        <p className="mt-4 text-[10px] uppercase tracking-wide text-slate-500">
                          Next time I take this setup, I will use
                        </p>
                        <div className="mt-1 grid gap-3 sm:grid-cols-3">
                          <Field label="Revised entry">
                            <input
                              type="number"
                              step="any"
                              value={rev.revisedEntry}
                              onChange={(e) => setReview(rt, { revisedEntry: e.target.value })}
                              className={`mt-1 ${fieldClass}`}
                            />
                          </Field>
                          <Field label="Revised stop">
                            <input
                              type="number"
                              step="any"
                              value={rev.revisedStop}
                              onChange={(e) => setReview(rt, { revisedStop: e.target.value })}
                              className={`mt-1 ${fieldClass}`}
                            />
                          </Field>
                          <Field label="Revised target">
                            <input
                              type="number"
                              step="any"
                              value={rev.revisedTarget}
                              onChange={(e) => setReview(rt, { revisedTarget: e.target.value })}
                              className={`mt-1 ${fieldClass}`}
                            />
                          </Field>
                        </div>

                        <div className="mt-3 space-y-3">
                          <Field label="What went well">
                            <textarea
                              value={rev.wentWell}
                              onChange={(e) => setReview(rt, { wentWell: e.target.value })}
                              rows={2}
                              className={`mt-1 resize-y ${fieldClass}`}
                            />
                          </Field>
                          <Field label="What went wrong">
                            <textarea
                              value={rev.wentWrong}
                              onChange={(e) => setReview(rt, { wentWrong: e.target.value })}
                              rows={2}
                              className={`mt-1 resize-y ${fieldClass}`}
                            />
                          </Field>
                          <Field label="What to learn">
                            <textarea
                              value={rev.lessons}
                              onChange={(e) => setReview(rt, { lessons: e.target.value })}
                              rows={2}
                              className={`mt-1 resize-y ${fieldClass}`}
                            />
                          </Field>
                        </div>

                        <div className="mt-3 flex items-center justify-between">
                          <span className="text-[10px] text-obsidian-muted">
                            {rt.review_status === 'reviewed' ? 'Reviewed' : 'Awaiting review'}
                          </span>
                          <button
                            type="button"
                            onClick={() => saveReview(rt)}
                            disabled={review.isPending}
                            className="rounded-lg border border-win-border bg-win-glow px-3 py-1.5 text-[11px] text-win disabled:opacity-50"
                          >
                            {review.isPending ? 'Saving…' : 'Save review'}
                          </button>
                        </div>
                      </section>
                    ) : (
                      <p className="text-[10px] text-obsidian-muted">
                        Still open — the review unlocks once this position is closed.
                      </p>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};

export default TradeLedger;

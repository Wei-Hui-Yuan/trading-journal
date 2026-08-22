'use client';

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  ArrowDownRight,
  ArrowUpRight,
  ChevronRight,
  ClipboardList,
  Link2,
  Loader2,
  NotebookPen,
  Pencil,
  Plus,
  Search,
  Trash2,
  Unlink,
  Wrench,
  X,
} from 'lucide-react';

import {
  useAnnotateTrade,
  useAttachPlan,
  useDeleteTrade,
  useDetachPlan,
  useOpenRoundTripCount,
  usePlans,
  useReviewPosition,
  useRoundTrips,
  useStrategies,
  useUpdateExecution,
} from '@/hooks/useTradeInbox';
import type { RoundTripFilters } from '@/hooks/useTradeInbox';
import type {
  PositionFill,
  RoundTrip,
  TradeDeleteResult,
  TradePlan,
  TradeSide,
} from '@/types/api';
import { computeDisciplineScore } from '@/lib/discipline';
import { RepairFillModal } from './RepairFillModal';
import { PlanChartView } from './PlanChart';
import { ConfirmDialog } from './ConfirmDialog';
import { usePendingActions } from './PendingActionProvider';

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

/** In-progress correction to one fill. Strings, so "empty" is representable. */
interface ExecutionDraft {
  direction: TradeSide;
  quantity: string;
  price: string;
  executedAt: string; // datetime-local, US market time
}

const BLANK_EXECUTION: ExecutionDraft = {
  direction: 'BUY',
  quantity: '',
  price: '',
  executedAt: '',
};

/**
 * An ISO instant as a datetime-local value in US market time.
 *
 * The field must round-trip: the backend reads a bare timestamp as
 * America/New_York, so rendering it in browser-local time would shift every
 * fill the moment it was saved from outside ET.
 */
function toMarketDateTimeLocal(iso: string): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(new Date(iso));
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? '00';
  const hour = get('hour') === '24' ? '00' : get('hour');
  return `${get('year')}-${get('month')}-${get('day')}T${hour}:${get('minute')}`;
}

/**
 * The side a fill actually was.
 *
 * `PositionFill` carries a role, not a direction: OPEN means "moved into the
 * position", which is a BUY on a long and a SELL on a short. Deriving it keeps
 * one source of truth rather than storing the same fact twice.
 */
function fillDirection(role: string, tradeDirection: string): TradeSide {
  const entry = (tradeDirection || 'BUY').toUpperCase() === 'BUY' ? 'BUY' : 'SELL';
  if (role === 'OPEN') return entry;
  return entry === 'BUY' ? 'SELL' : 'BUY';
}

function draftFromFill(fill: PositionFill, tradeDirection: string): ExecutionDraft {
  return {
    direction: fillDirection(fill.role, tradeDirection),
    quantity: formatQuantity(fill.quantity),
    price: String(fill.price),
    executedAt: toMarketDateTimeLocal(fill.executed_at),
  };
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
 * The collapsed row: one round trip, scannable at a glance.
 *
 * Extracted and memoised because it is rendered once per round trip and the
 * ledger re-renders on every keystroke — into the search box, and into any
 * field of the one expanded row, since the draft state for those lives on the
 * parent. Measured at 128 rows, a single keystroke in a plan field cost ~9 ms
 * and a search keystroke up to ~27 ms, essentially all of it React
 * reconciling 127 rows whose output had not changed.
 *
 * The props are chosen so that stays true: `rt` comes straight from the query
 * cache and keeps its identity between renders, `strategyName` and
 * `isExpanded` are primitives, and `onToggle` is a stable callback taking the
 * key rather than a fresh closure per row. Passing an inline arrow here would
 * change on every render and defeat the memo entirely.
 */
interface RoundTripHeaderProps {
  rt: RoundTrip;
  isExpanded: boolean;
  strategyName: string | null;
  onToggle: (key: string) => void;
}

const RoundTripHeader = React.memo<RoundTripHeaderProps>(function RoundTripHeader({
  rt,
  isExpanded,
  strategyName,
  onToggle,
}) {
  const isBuy = rt.direction === 'BUY';
  const isOpen = rt.kind === 'open';

  // Drag is measured against capital committed, not against P&L. Dividing by
  // the price move made a scratch trade — three cents of movement against a
  // $0.71 fee — report 1884%, which says nothing about how expensive the trade
  // actually was to hold.
  const capitalCommitted = Math.abs((rt.entry_price ?? 0) * (rt.quantity ?? 0));
  // Magnitude, so the label carries the direction: a rebate reads
  // "Credit: $0.05 (0.02%)" rather than "Credit: $0.05 (-0.02%)".
  const feeDragPct =
    capitalCommitted > 0 && rt.commission !== null
      ? (Math.abs(rt.commission) / capitalCommitted) * 100
      : 0;
  const feeDragText =
    feeDragPct > 0 && feeDragPct < 0.01
      ? '< 0.01'
      : feeDragPct < 1
        ? feeDragPct.toFixed(2)
        : feeDragPct.toFixed(1);
  // Commission is stored as a cost, so a negative one is a rebate IBKR passed
  // through — real money received, and not something to show in fee amber.
  const isCredit = rt.commission !== null && rt.commission < 0;

  return (
    <button
      type="button"
      onClick={() => onToggle(rt.key)}
      className="flex w-full items-center gap-3 px-4 py-3 text-left"
    >
      <ChevronRight
        className={`h-3.5 w-3.5 shrink-0 text-obsidian-muted transition-transform ${
          isExpanded ? 'rotate-90' : ''
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

      {rt.commission !== null && rt.commission !== 0 && (
        <span
          className={`hidden shrink-0 rounded border px-1.5 py-0.5 font-mono text-[10px] sm:inline ${
            isCredit
              ? 'border-win/20 bg-win/10 text-win'
              : 'border-amber-500/20 bg-amber-500/10 text-amber-400'
          }`}
          title={
            isCredit
              ? `Exchange rebate: $${Math.abs(rt.commission).toFixed(2)} received ` +
                `(${feeDragPct.toFixed(2)}% of $${capitalCommitted.toFixed(2)} capital committed)`
              : `Broker commission: $${rt.commission.toFixed(2)} ` +
                `(${feeDragPct.toFixed(2)}% of $${capitalCommitted.toFixed(2)} capital committed)`
          }
        >
          {isCredit ? 'Credit' : 'Fee'}: ${Math.abs(rt.commission).toFixed(2)} ({feeDragText}%)
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

      {/* Visible without expanding, because "was this planned?" is
          the question you scan the ledger for. */}
      {rt.plan_id && (
        <span
          className="inline-flex shrink-0 items-center gap-1 rounded bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-300"
          title="Entered from a pre-trade plan"
        >
          <ClipboardList className="h-2.5 w-2.5" />
          Planned
        </span>
      )}
      {rt.has_hand_added_fills && (
        <span
          className="inline-flex shrink-0 items-center gap-1 rounded bg-slate-700/40 px-1.5 py-0.5 text-[10px] text-slate-400"
          title="Contains a fill typed in by hand, not reported by IBKR"
        >
          <Wrench className="h-2.5 w-2.5" />
          Hand-added
        </span>
      )}

      <span className="ml-auto shrink-0 font-mono text-[10px] text-obsidian-muted">
        {dateFormatter.format(new Date(rt.exit_time ?? rt.entry_time))}
      </span>
    </button>
  );
});

const planTimeFormatter = new Intl.DateTimeFormat('en-US', {
  month: 'short',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
  timeZone: 'America/New_York',
});

/**
 * What you committed to, against what the broker actually did.
 *
 * Only rendered when a plan is attached, and that condition is the whole
 * point. The same `planned_entry` column can hold two very different things: a
 * number committed to before the outcome was known, or one typed into this
 * form afterwards. Only the first is evidence about your process, and only an
 * attached plan carries a timestamp proving which it is.
 */
/**
 * Where the P&L figure came from: price move, cost of trading, what is left.
 *
 * The headline number is net of commission (migration 020). It used to be the
 * price move alone, which is why nothing in this journal ever tied out against
 * a broker statement — $102.56 of commission had been charged to the account
 * and appeared in none of these figures.
 *
 * Shown as three lines rather than folded into one, because cost is a thing
 * the trader controls — through size, through how often they trade — and it
 * cannot be managed while it is invisible. On this account a 0.1-share fill
 * paid 1.00% of notional to execute.
 */
const PnlBreakdown: React.FC<{ rt: RoundTrip }> = ({ rt }) => {
  const { gross_pnl: gross, commission, realized_pnl: net } = rt;
  if (gross === null || commission === null || net === null) return null;

  const money = (n: number) => `${n >= 0 ? '+' : '−'}$${Math.abs(n).toFixed(2)}`;
  // A rebate is real money received, and reads as a negative cost.
  const isRebate = commission < 0;

  return (
    <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 font-mono text-[11px]">
      <span className="text-obsidian-muted">
        Gross{' '}
        <span className={gross >= 0 ? 'text-win/70' : 'text-loss/70'}>
          {money(gross)}
        </span>
      </span>
      <span className="text-obsidian-muted">
        {isRebate ? 'Rebate' : 'Commission'}{' '}
        <span className={isRebate ? 'text-win/70' : 'text-loss/70'}>
          {isRebate ? '+' : '−'}${Math.abs(commission).toFixed(2)}
        </span>
      </span>
      <span className="text-obsidian-muted">
        Net{' '}
        <span className={`font-semibold ${net >= 0 ? 'text-win' : 'text-loss'}`}>
          {money(net)}
        </span>
      </span>
      {/* The case worth catching on sight: the price move was profitable and
          the trade was not. */}
      {gross > 0 && net <= 0 && (
        <span className="rounded border border-loss/30 bg-loss/5 px-1.5 py-0.5 text-[10px] text-loss">
          a winner before costs
        </span>
      )}
    </div>
  );
};

const PlanVsExecution: React.FC<{
  rt: RoundTrip;
  onUnlink: () => void;
  unlinking: boolean;
}> = ({ rt, onUnlink, unlinking }) => {
  const slip = rt.entry_slippage;
  const price = (n: number | null | undefined) =>
    n === null || n === undefined ? '—' : n < 1 ? n.toFixed(4) : n.toFixed(2);

  return (
    <section className="rounded-lg border border-amber-500/25 bg-amber-500/[0.03] p-3">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <ClipboardList className="h-3.5 w-3.5 text-amber-400" />
        <h4 className="text-[11px] font-semibold uppercase tracking-wider text-amber-300">
          Planned before entry
        </h4>
        {rt.plan_created_at && (
          <span className="font-mono text-[10px] text-obsidian-muted">
            written {planTimeFormatter.format(new Date(rt.plan_created_at))} ET
          </span>
        )}
        <button
          type="button"
          onClick={onUnlink}
          disabled={unlinking}
          title="Unlink this plan. It returns to the dock and can attach elsewhere; the values it copied stay on the trade."
          className="ml-auto inline-flex items-center gap-1 rounded border border-obsidian-border px-2 py-0.5 text-[10px] text-obsidian-muted transition-colors hover:border-slate-600 hover:text-slate-200 disabled:opacity-50"
        >
          {unlinking ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <Unlink className="h-3 w-3" />
          )}
          Unlink plan
        </button>
      </div>

      <div className="grid gap-x-6 gap-y-2 text-[11px] sm:grid-cols-2">
        <div className="space-y-1.5">
          <div className="flex justify-between font-mono">
            <span className="text-obsidian-muted">Planned entry</span>
            <span className="text-slate-300">{price(rt.planned_entry)}</span>
          </div>
          <div className="flex justify-between font-mono">
            <span className="text-obsidian-muted">Actual entry</span>
            <span className="text-slate-100">{price(rt.entry_price)}</span>
          </div>
          {slip !== null && slip !== undefined && (
            <div className="flex justify-between font-mono">
              <span className="text-obsidian-muted">Slippage</span>
              {/* Signed so positive always means better than planned — which
                  is the opposite arithmetic on a short, and is why this is
                  computed server-side rather than subtracted here. */}
              <span className={slip >= 0 ? 'text-win' : 'text-loss'}>
                {slip >= 0 ? '+' : ''}
                {slip.toFixed(4).replace(/0+$/, '').replace(/\.$/, '')}
                <span className="ml-1 text-obsidian-muted">
                  {slip >= 0 ? 'better' : 'worse'}
                </span>
              </span>
            </div>
          )}
        </div>

        <div className="space-y-1.5">
          <div className="flex justify-between font-mono">
            <span className="text-obsidian-muted">Planned R</span>
            <span className="text-slate-300">
              {rt.planned_r_multiple === null
                ? '—'
                : `${rt.planned_r_multiple.toFixed(2)}R`}
            </span>
          </div>
          <div className="flex justify-between font-mono">
            <span className="text-obsidian-muted">Realised R</span>
            <span
              className={
                rt.r_multiple === null
                  ? 'text-obsidian-muted'
                  : rt.r_multiple >= 0
                    ? 'text-win'
                    : 'text-loss'
              }
            >
              {rt.r_multiple === null
                ? 'still open'
                : `${rt.r_multiple >= 0 ? '+' : ''}${rt.r_multiple.toFixed(2)}R`}
            </span>
          </div>
        </div>
      </div>

      {/* What you were looking at when you decided. The numbers above say
          whether you followed the plan; this is the only thing that says
          whether the plan was reasonable — and it is why it sits inside the
          "planned before entry" block rather than beside the review, which
          was written afterwards. */}
      {rt.plan_has_chart && rt.plan_id && (
        <div className="mt-3 border-t border-amber-500/15 pt-3">
          <p className="mb-1.5 text-[10px] uppercase tracking-wide text-obsidian-muted">
            Chart at entry
          </p>
          <PlanChartView planId={rt.plan_id} />
        </div>
      )}
    </section>
  );
};

/**
 * The escape hatch for the normal plan-then-fill-then-sync order.
 *
 * Auto-attach on sync requires the plan to predate the fill -- a plan written
 * even a minute after the broker filled the order describes a trade that
 * already happened, and is correctly refused. This is for exactly that case:
 * the order went in first, the plan got written on the way to syncing, and
 * the two need linking by hand. The backend enforces ticker and direction;
 * candidates are pre-filtered to those here so a mismatched pick fails as
 * "nothing to choose from" rather than a 422 after the fact.
 */
const AttachPlanPanel: React.FC<{
  rt: RoundTrip;
  plans: TradePlan[] | undefined;
  attachingPlanId: string | null;
  onAttach: (planId: string) => void;
}> = ({ rt, plans, attachingPlanId, onAttach }) => {
  if (!rt.plan_trade_id) return null;

  const candidates = (plans ?? []).filter(
    (p) => p.ticker === rt.symbol && p.direction === rt.direction
  );
  const price = (n: number | null) => (n === null ? '—' : n < 1 ? n.toFixed(4) : n.toFixed(2));

  return (
    <section className="rounded-lg border border-dashed border-obsidian-border p-3">
      <div className="mb-2 flex items-center gap-2">
        <Link2 className="h-3.5 w-3.5 text-obsidian-muted" />
        <h4 className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
          No plan attached
        </h4>
      </div>

      {candidates.length === 0 ? (
        <p className="text-[11px] text-obsidian-muted">
          No open plan for {rt.symbol} {rt.direction}
          {plans && plans.length > 0
            ? ` (${plans.length} open plan${plans.length === 1 ? ' exists' : 's exist'} for other tickers or directions).`
            : '.'}
        </p>
      ) : (
        <ul className="space-y-1.5">
          {candidates.map((plan) => (
            <li
              key={plan.id}
              className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded border border-obsidian-border bg-obsidian-bg px-2.5 py-1.5 text-[11px]"
            >
              <span className="font-mono text-slate-300">
                entry {price(plan.planned_entry)}
              </span>
              <span className="font-mono text-obsidian-muted">
                stop {price(plan.stop_loss)}
              </span>
              <span className="font-mono text-obsidian-muted">
                target {price(plan.take_profit)}
              </span>
              {plan.created_at && (
                <span className="font-mono text-[10px] text-obsidian-muted">
                  written {planTimeFormatter.format(new Date(plan.created_at))} ET
                </span>
              )}
              <button
                type="button"
                onClick={() => onAttach(plan.id)}
                disabled={attachingPlanId === plan.id}
                className="ml-auto inline-flex items-center gap-1 rounded border border-amber-500/30 px-2 py-0.5 text-[10px] text-amber-300 transition-colors hover:border-amber-500/60 hover:bg-amber-500/10 disabled:opacity-50"
              >
                {attachingPlanId === plan.id ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <Link2 className="h-3 w-3" />
                )}
                Attach
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
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
  const [filter, setFilter] = useState<Filter>('all');
  const [query, setQuery] = useState('');
  const [selectedStrategy, setSelectedStrategy] = useState<string>('all');

  // The search box drives a request now, so the value that reaches the server
  // trails the one being typed. Without this every keystroke is a query, and
  // "AAPL" costs four of them -- three for prefixes nobody wanted to see.
  const [searchTerm, setSearchTerm] = useState('');
  useEffect(() => {
    const timer = setTimeout(() => setSearchTerm(query), 250);
    return () => clearTimeout(timer);
  }, [query]);

  // Sent to the server rather than applied to what came back. Narrowing the
  // fetched rows would narrow the PAGE, not the journal: a search would match
  // on the loaded page and miss identical rows on the next one.
  const filters = useMemo<RoundTripFilters>(
    () => ({
      kind: filter === 'all' ? undefined : filter,
      strategy: selectedStrategy === 'all' ? undefined : selectedStrategy,
      search: searchTerm,
    }),
    [filter, selectedStrategy, searchTerm]
  );

  // Distinguishes "nothing matches what you asked for" from "there is
  // nothing here at all" -- the same isFiltered-driven split
  // InvestmentTable.tsx uses for its own empty state.
  const isFiltered = filter !== 'all' || selectedStrategy !== 'all' || searchTerm !== '';

  const {
    flat: visible,
    isLoading,
    error,
    hasNextPage,
    fetchNextPage,
    isFetchingNextPage,
    // True exactly while the rows on screen belong to the PREVIOUS filter.
    // The ledger no longer blanks between filters, which means without a
    // signal here it would show stale rows as though they were results — and
    // a round trip to the API is slow enough to read one and believe it.
    isPlaceholderData: isSwappingFilter,
  } = useRoundTrips(filters);
  // Independent of the filter above — see the hook.
  const openCount = useOpenRoundTripCount();
  // Neither this, openCount above, nor openPlans below has loading/error UI
  // of its own -- each degrades to "empty" (0, an unpopulated dropdown, no
  // attach candidates) while pending, or if the request fails outright.
  // Deliberate: these are secondary reads the page can do without, unlike
  // the round trips query above, whose own loading/error states this
  // component does handle explicitly.
  const { data: strategies } = useStrategies();
  const annotate = useAnnotateTrade();
  const review = useReviewPosition();
  const deleteTradeMutation = useDeleteTrade();
  const updateExecutionMutation = useUpdateExecution();
  // What a deletion actually did. Surfaced because the side effects reach
  // beyond the row that was clicked.
  const [deleteNotice, setDeleteNotice] = useState<string | null>(null);
  // The fill awaiting confirmation, with the round trip it belongs to — the
  // dialog needs both to say what dissolving that round trip would cost.
  const [confirmingFill, setConfirmingFill] = useState<{
    fill: PositionFill;
    roundTrip: RoundTrip;
  } | null>(null);
  const { schedule, isPending } = usePendingActions();

  const [expanded, setExpanded] = useState<string | null>(null);
  const detachMutation = useDetachPlan();
  const attachMutation = useAttachPlan();
  // Only fetched to populate the "attach a plan" picker on unplanned rows --
  // never gated on whether one is expanded, since that would mean an extra
  // round trip every time a row opens rather than one shared list.
  const { data: openPlans } = usePlans('OPEN');
  // Keyed by round trip, like planDrafts/reviewDrafts below. A failed
  // attach/detach/plan-save/review-save must stay put on the row it happened
  // to, not read as though it belongs to whichever row is expanded next --
  // only one row is ever expanded at a time, but that is not the same
  // guarantee as this error being cleared when the user moves on.
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  // Which fill is being corrected, and the in-progress values. Kept as strings
  // for the same reason the manual form does: a controlled number input has to
  // represent "empty" and mid-typing states that Number() would mangle.
  const [editingFillId, setEditingFillId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState<ExecutionDraft>(BLANK_EXECUTION);
  // Ticker to prefill the Add-fill form with, or null when it is closed.
  const [addingFor, setAddingFor] = useState<string | null>(null);
  const [planDrafts, setPlanDrafts] = useState<Record<string, PlanDraft>>({});
  const [reviewDrafts, setReviewDrafts] = useState<Record<string, ReviewDraft>>({});

  // Id -> name, so each row is an O(1) lookup instead of a scan.
  const strategyNameById = useMemo(
    () => new Map((strategies ?? []).map((s) => [s.id, s.name])),
    [strategies]
  );

  // Stable across renders, which is what lets RoundTripHeader's memo hold. An
  // inline `() => setExpanded(...)` would be a new function every render and
  // every row would re-render regardless.
  const toggleExpanded = useCallback((key: string) => {
    setExpanded((current) => (current === key ? null : key));
  }, []);

  // A fill left mid-edit must not silently resume "editing" the next time its
  // row is expanded. editingFillId is a single, ledger-wide value rather than
  // one keyed per row -- there is never more than one expanded row to hold
  // it -- so as soon as the previously-expanded row stops being the one
  // showing (collapsed outright, or replaced by a different row opening) any
  // in-progress fill edit belongs to a row the user can no longer see, and is
  // discarded the same way clicking Cancel would.
  const previouslyExpandedRef = useRef<string | null>(null);
  useEffect(() => {
    const previous = previouslyExpandedRef.current;
    previouslyExpandedRef.current = expanded;
    if (previous) setEditingFillId(null);
  }, [expanded]);

  const planOf = (rt: RoundTrip) => planDrafts[rt.key] ?? planDraftFrom(rt);
  const reviewOf = (rt: RoundTrip) => reviewDrafts[rt.key] ?? reviewDraftFrom(rt);

  const setRowError = (rt: RoundTrip, message: string | null) =>
    setRowErrors((prev) => {
      if (message === null) {
        if (!(rt.key in prev)) return prev;
        const { [rt.key]: _removed, ...rest } = prev;
        return rest;
      }
      return { ...prev, [rt.key]: message };
    });

  const setPlan = (rt: RoundTrip, patch: Partial<PlanDraft>) =>
    setPlanDrafts((prev) => ({ ...prev, [rt.key]: { ...planOf(rt), ...patch } }));
  const setReview = (rt: RoundTrip, patch: Partial<ReviewDraft>) =>
    setReviewDrafts((prev) => ({ ...prev, [rt.key]: { ...reviewOf(rt), ...patch } }));

  const savePlan = (rt: RoundTrip) => {
    if (!rt.plan_trade_id) return;
    const d = planOf(rt);
    setRowError(rt, null);
    annotate.mutate(
      {
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
      },
      {
        onSuccess: () => setDeleteNotice(`${rt.symbol}: plan saved.`),
        onError: (err) => setRowError(rt, err.message),
      }
    );
  };

  const saveReview = (rt: RoundTrip) => {
    if (!rt.position_id) return;
    const d = reviewOf(rt);
    setRowError(rt, null);
    review.mutate(
      {
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
      },
      {
        onSuccess: () => setDeleteNotice(`${rt.symbol}: review saved.`),
        onError: (err) => setRowError(rt, err.message),
      }
    );
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
        {error instanceof Error ? error.message : 'Failed to load journal.'}
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
            className={`${fieldClass} pl-8 pr-8`}
          />
          {isSwappingFilter && (
            <Loader2
              aria-hidden
              className="absolute right-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 animate-spin text-obsidian-muted"
            />
          )}
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
        <select
          value={selectedStrategy}
          onChange={(e) => setSelectedStrategy(e.target.value)}
          className="rounded-lg border border-obsidian-border bg-obsidian-bg px-3 py-2 text-[11px] uppercase tracking-wider text-obsidian-muted transition-colors focus:border-slate-600 focus:outline-none"
        >
          <option value="all">All Strategies</option>
          <option value="unassigned">Unassigned</option>
          {(strategies ?? []).map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
      </div>

      {deleteNotice && (
        <div className="flex items-start gap-2 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-xs text-amber-300">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span className="flex-1">{deleteNotice}</span>
          <button
            type="button"
            onClick={() => setDeleteNotice(null)}
            className="text-amber-400/70 hover:text-amber-200"
          >
            Dismiss
          </button>
        </div>
      )}

      {visible.length === 0 ? (
        <p className="py-16 text-center text-sm text-obsidian-muted">
          {isFiltered ? 'No trades match this filter.' : 'Nothing in the journal yet.'}
        </p>
      ) : (
        <div
          aria-busy={isSwappingFilter}
          className={`space-y-2 transition-opacity duration-150 ${
            isSwappingFilter ? 'opacity-40' : 'opacity-100'
          }`}
        >
          {visible.map((rt) => {
            const isExpanded = expanded === rt.key;
            // Built once per render rather than per row. `strategies.find()`
            // here was O(rows x strategies) for a lookup the Map does in O(1).
            const strategyName = rt.strategy_id
              ? strategyNameById.get(rt.strategy_id) ?? null
              : null;
            // Only the expanded row reads these, so only it pays for them.
            // Both allocate a fresh object, which would also defeat any memo
            // they were passed through.
            const plan = isExpanded ? planOf(rt) : null;
            const rev = isExpanded ? reviewOf(rt) : null;
            const disciplineScore = isExpanded
              ? computeDisciplineScore(rt.disciplines)
              : null;

            return (
              <div key={rt.key} className="rounded-xl border border-obsidian-border bg-obsidian-card">
                <RoundTripHeader
                  rt={rt}
                  isExpanded={isExpanded}
                  strategyName={strategyName}
                  onToggle={toggleExpanded}
                />

                {plan && rev && (
                  <div className="space-y-6 border-t border-obsidian-border px-4 py-4">
                    {/* What the headline P&L on the row above is made of. First,
                        because it explains a number the user has already read. */}
                    <PnlBreakdown rt={rt} />

                    {/* ------- PLAN vs EXECUTION, or a way to link one ------- */}
                    {rt.plan_id ? (
                      <PlanVsExecution
                        rt={rt}
                        unlinking={
                          detachMutation.isPending &&
                          detachMutation.variables === rt.plan_trade_id
                        }
                        onUnlink={() => {
                          if (!rt.plan_trade_id) return;
                          setRowError(rt, null);
                          detachMutation.mutate(rt.plan_trade_id, {
                            onError: (err) => setRowError(rt, err.message),
                          });
                        }}
                      />
                    ) : (
                      <AttachPlanPanel
                        rt={rt}
                        plans={openPlans}
                        attachingPlanId={
                          attachMutation.isPending &&
                          attachMutation.variables?.tradeId === rt.plan_trade_id
                            ? attachMutation.variables.planId
                            : null
                        }
                        onAttach={(planId) => {
                          if (!rt.plan_trade_id) return;
                          setRowError(rt, null);
                          attachMutation.mutate(
                            { tradeId: rt.plan_trade_id, planId },
                            { onError: (err) => setRowError(rt, err.message) }
                          );
                        }}
                      />
                    )}
                    {rowErrors[rt.key] && (
                      <div className="flex items-start text-xs text-loss">
                        <AlertCircle className="mr-1.5 mt-px h-3.5 w-3.5 shrink-0" />
                        <span>{rowErrors[rt.key]}</span>
                      </div>
                    )}

                    {/* ---------------- THE PLAN ---------------- */}
                    <section>
                      <SectionHeading
                        title={rt.plan_id ? 'The Plan · as recorded' : 'The Plan'}
                        blurb={
                          rt.plan_id
                            ? 'Filled in from the attached plan. Editing here changes the trade, not the plan it came from.'
                            : 'Written at entry. Saved on this round trip’s opening execution, so a scale-in has one stop, not several.'
                        }
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
                            {rt.fills
                              // Hidden while its undo window runs, so the row
                              // does not sit there looking undeleted and
                              // invite a second click.
                              .filter((f) => !isPending(`trade:${f.trade_id}`))
                              .map((f) =>
                              editingFillId === f.id ? (
                                <tr
                                  key={f.id}
                                  className="border-t border-obsidian-border/60 bg-obsidian-bg/40"
                                >
                                  <td className="py-1.5 pr-2">
                                    <select
                                      value={editDraft.direction}
                                      onChange={(e) =>
                                        setEditDraft({
                                          ...editDraft,
                                          direction: e.target.value as TradeSide,
                                        })
                                      }
                                      className="w-full rounded border border-obsidian-border bg-obsidian-bg px-1 py-0.5 text-[10px] text-slate-200"
                                    >
                                      <option value="BUY">BUY</option>
                                      <option value="SELL">SELL</option>
                                    </select>
                                  </td>
                                  <td className="py-1.5 pr-2">
                                    <input
                                      type="number"
                                      step="0.00000001"
                                      min="0"
                                      value={editDraft.quantity}
                                      onChange={(e) =>
                                        setEditDraft({ ...editDraft, quantity: e.target.value })
                                      }
                                      className="w-20 rounded border border-obsidian-border bg-obsidian-bg px-1 py-0.5 text-[11px] text-slate-200"
                                    />
                                  </td>
                                  <td className="py-1.5 pr-2">
                                    <input
                                      type="number"
                                      step="0.0001"
                                      min="0"
                                      value={editDraft.price}
                                      onChange={(e) =>
                                        setEditDraft({ ...editDraft, price: e.target.value })
                                      }
                                      className="w-24 rounded border border-obsidian-border bg-obsidian-bg px-1 py-0.5 text-[11px] text-slate-200"
                                    />
                                  </td>
                                  <td className="py-1.5 pr-2">
                                    <input
                                      type="datetime-local"
                                      value={editDraft.executedAt}
                                      onChange={(e) =>
                                        setEditDraft({ ...editDraft, executedAt: e.target.value })
                                      }
                                      className="rounded border border-obsidian-border bg-obsidian-bg px-1 py-0.5 text-[10px] text-slate-200"
                                    />
                                  </td>
                                  <td className="py-1.5 text-right">
                                    <div className="inline-flex gap-1">
                                      <button
                                        type="button"
                                        onClick={() => {
                                          const quantity = Number(editDraft.quantity);
                                          const price = Number(editDraft.price);
                                          if (!Number.isFinite(quantity) || quantity <= 0) {
                                            setDeleteNotice('Quantity must be greater than zero.');
                                            return;
                                          }
                                          if (!Number.isFinite(price) || price <= 0) {
                                            setDeleteNotice('Price must be greater than zero.');
                                            return;
                                          }
                                          updateExecutionMutation.mutate(
                                            {
                                              id: f.trade_id,
                                              payload: {
                                                direction: editDraft.direction,
                                                quantity,
                                                price,
                                                // Sent bare; the backend anchors
                                                // it to America/New_York.
                                                execution_time: editDraft.executedAt
                                                  ? `${editDraft.executedAt}:00`
                                                  : undefined,
                                              },
                                            },
                                            {
                                              onSuccess: (result) => {
                                                setEditingFillId(null);
                                                setDeleteNotice(
                                                  `${result.ticker}: fill corrected. ` +
                                                    `${result.positions_removed} round trip(s) rebuilt as ${result.positions_rebuilt}` +
                                                    (result.reviews_discarded > 0
                                                      ? `, ${result.reviews_discarded} review(s) discarded.`
                                                      : '.')
                                                );
                                              },
                                              onError: (err) => setDeleteNotice(err.message),
                                            }
                                          );
                                        }}
                                        disabled={updateExecutionMutation.isPending}
                                        className="rounded bg-win-glow px-2 py-0.5 text-[10px] text-win border border-win-border hover:bg-win/20 transition-colors disabled:opacity-50 font-sans"
                                      >
                                        {updateExecutionMutation.isPending ? 'Saving…' : 'Save'}
                                      </button>
                                      <button
                                        type="button"
                                        onClick={() => setEditingFillId(null)}
                                        disabled={updateExecutionMutation.isPending}
                                        aria-label="Cancel edit"
                                        className="rounded border border-obsidian-border px-1.5 py-0.5 text-obsidian-muted hover:text-slate-200 transition-colors disabled:opacity-50"
                                      >
                                        <X className="h-3 w-3" />
                                      </button>
                                    </div>
                                  </td>
                                </tr>
                              ) : (
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
                                      setEditingFillId(f.id);
                                      setEditDraft(draftFromFill(f, rt.direction));
                                      setDeleteNotice(null);
                                    }}
                                    disabled={updateExecutionMutation.isPending}
                                    className="mr-1 inline-flex items-center gap-1 rounded border border-obsidian-border px-2 py-0.5 text-[10px] text-obsidian-muted hover:text-slate-200 hover:border-slate-600 transition-colors disabled:opacity-50 font-sans"
                                    title="Correct this fill"
                                  >
                                    <Pencil className="h-3 w-3" />
                                    <span>Edit</span>
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() =>
                                      setConfirmingFill({ fill: f, roundTrip: rt })
                                    }
                                    disabled={deleteTradeMutation.isPending}
                                    className="inline-flex items-center gap-1 rounded bg-loss/10 px-2 py-0.5 text-[10px] text-loss border border-loss/20 hover:bg-loss/20 transition-colors disabled:opacity-50 font-sans"
                                    title="Delete execution fill"
                                  >
                                    <Trash2 className="h-3 w-3" />
                                    <span>Delete</span>
                                  </button>
                                </td>
                              </tr>
                              )
                            )}
                          </tbody>
                        </table>
                      </div>

                      {/* A missing fill is corrected by adding it to the
                          ticker, not to this round trip: positions are derived
                          from executions, so FIFO decides which trip it joins.
                          Reuses the manual form, calculator and all. */}
                      <button
                        type="button"
                        onClick={() => setAddingFor(rt.symbol)}
                        className="mt-2 inline-flex items-center gap-1.5 rounded-lg border border-obsidian-border bg-obsidian-bg px-2.5 py-1 text-[10px] text-obsidian-muted hover:text-slate-200 hover:border-slate-600 transition-colors"
                      >
                        <Plus className="h-3 w-3" />
                        Add a missing {rt.symbol} fill
                      </button>
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
                          <span className="flex items-center gap-2 text-[10px] text-obsidian-muted">
                            {rt.review_status === 'reviewed' ? 'Reviewed' : 'Awaiting review'}
                            {/* Null (never checked against any rule) shows
                                nothing rather than a misleading 0% — see
                                computeDisciplineScore. */}
                            {disciplineScore !== null && (
                              <span
                                className="font-mono text-slate-300"
                                title={`${rt.disciplines.filter((d) => d.followed).length} of ${rt.disciplines.length} answered rules followed`}
                              >
                                · {disciplineScore}% compliant
                              </span>
                            )}
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

      {/* Only closed round trips page, so this appears once the server has
          more history than has been asked for. Open exposure is never behind
          it — that always arrives complete on the first page. */}
      {hasNextPage && (
        <div className="flex justify-center pt-4">
          <button
            type="button"
            onClick={() => fetchNextPage()}
            disabled={isFetchingNextPage}
            className="border border-obsidian-line px-4 py-2 text-[11px] uppercase tracking-wider text-obsidian-muted transition-colors hover:text-slate-200 disabled:opacity-50"
          >
            {isFetchingNextPage ? 'Loading…' : 'Load older trades'}
          </button>
        </div>
      )}

      {/* Prefilled with the ticker whose Add button was pressed. Mounted once
          at the root rather than per row, so only one can ever be open. */}
      <RepairFillModal
        open={addingFor !== null}
        presetSymbol={addingFor ?? undefined}
        onClose={() => setAddingFor(null)}
      />

      {/* A fill is rarely deletable in isolation: removing one from a closed
          round trip dissolves it, and FIFO re-matching rebuilds whatever the
          remaining fills now form — discarding the review written against the
          old shape. Said up front rather than discovered afterwards. */}
      <ConfirmDialog
        open={confirmingFill !== null}
        title="Delete this execution?"
        confirmLabel="Delete fill"
        cancelLabel="Keep it"
        onCancel={() => setConfirmingFill(null)}
        onConfirm={() => {
          const target = confirmingFill;
          setConfirmingFill(null);
          if (!target) return;
          const { fill, roundTrip } = target;
          setDeleteNotice(null);
          schedule({
            id: `trade:${fill.trade_id}`,
            label: `${roundTrip.symbol} fill deleted`,
            detail: `${fill.role.toLowerCase()} ${formatQuantity(fill.quantity)} @ ${fill.price}`,
            commit: () => deleteTradeMutation.mutateAsync(fill.trade_id),
            onCommitted: (result) => {
              const r = result as TradeDeleteResult;
              setDeleteNotice(
                `${roundTrip.symbol}: ${r.positions_removed} round trip(s) removed, ${r.positions_rebuilt} rebuilt` +
                  (r.reviews_discarded > 0
                    ? `, ${r.reviews_discarded} review(s) discarded.`
                    : '.')
              );
            },
            onError: (err) => setDeleteNotice(err.message),
          });
        }}
      >
        <p>
          <span className="text-slate-100">
            {confirmingFill?.fill.role.toLowerCase()}{' '}
            {confirmingFill ? formatQuantity(confirmingFill.fill.quantity) : ''}{' '}
            {confirmingFill?.roundTrip.symbol}
          </span>{' '}
          @ {confirmingFill?.fill.price}
        </p>
        {confirmingFill?.roundTrip.kind === 'closed' && (
          <p>
            This fill belongs to a closed round trip. Deleting it rebuilds{' '}
            {confirmingFill.roundTrip.symbol}&apos;s round trips from the
            remaining executions, and{' '}
            <span className="text-loss">the review attached to it is discarded</span>.
          </p>
        )}
        <p className="text-obsidian-muted">
          If this fill came from IBKR it is also suppressed, so a later sync
          cannot add it back.
        </p>
      </ConfirmDialog>
    </div>
  );
};

export default TradeLedger;

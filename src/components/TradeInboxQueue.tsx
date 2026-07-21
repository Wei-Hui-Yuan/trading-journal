'use client';

import React, { useState } from 'react';
import {
  AlertCircle,
  ArrowDownRight,
  ArrowUpRight,
  CheckCircle2,
  ChevronRight,
  Inbox,
  Loader2,
} from 'lucide-react';

import {
  usePendingPositions,
  useReviewPosition,
  useStrategies,
} from '@/hooks/useTradeInbox';
import { PositionFills } from '@/components/PositionFills';
import type { Position, PositionReviewPayload } from '@/types/api';

const GRADES = ['A', 'B', 'C', 'D', 'F'] as const;

/** Draft checklist state for one card, before it is submitted. */
interface ReviewDraft {
  strategy_id: string;
  tag_hard_sl: boolean;
  tag_retest: boolean;
  tag_plan_compliant: boolean;
  trade_grade: string;
  // The post-mortem, asked as three questions. One combined box reliably
  // collapses into only ever recording what went wrong.
  review_went_well: string;
  review_went_wrong: string;
  review_lessons: string;
}

const emptyDraft: ReviewDraft = {
  strategy_id: '',
  tag_hard_sl: false,
  tag_retest: false,
  tag_plan_compliant: false,
  trade_grade: '',
  review_went_well: '',
  review_went_wrong: '',
  review_lessons: '',
};

const currency = (value: number) =>
  `${value >= 0 ? '+' : '-'}$${Math.abs(value).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;

const formatDateTime = (iso: string) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString('en-US', {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
};

export function TradeInboxQueue() {
  const positionsQuery = usePendingPositions();
  const strategiesQuery = useStrategies();
  const reviewMutation = useReviewPosition();

  // Drafts are keyed by position id so each card edits independently.
  const [drafts, setDrafts] = useState<Record<string, ReviewDraft>>({});
  // Which card the last mutation error belongs to.
  const [errorFor, setErrorFor] = useState<string | null>(null);
  // Only one execution drill-down open at a time, so the queue stays scannable.
  const [expandedFills, setExpandedFills] = useState<string | null>(null);

  const draftFor = (id: string): ReviewDraft => drafts[id] ?? emptyDraft;

  const patchDraft = (id: string, patch: Partial<ReviewDraft>) => {
    setDrafts((prev) => ({
      ...prev,
      [id]: { ...(prev[id] ?? emptyDraft), ...patch },
    }));
  };

  const handleSubmit = (position: Position) => {
    const draft = draftFor(position.id);

    // Send only what the user actually set; the backend applies just the keys
    // present, so omitting a field leaves it untouched rather than nulling it.
    const payload: PositionReviewPayload = {
      tag_hard_sl: draft.tag_hard_sl,
      tag_retest: draft.tag_retest,
      tag_plan_compliant: draft.tag_plan_compliant,
    };
    if (draft.strategy_id) payload.strategy_id = draft.strategy_id;
    if (draft.trade_grade) payload.trade_grade = draft.trade_grade;
    if (draft.review_went_well.trim())
      payload.review_went_well = draft.review_went_well.trim();
    if (draft.review_went_wrong.trim())
      payload.review_went_wrong = draft.review_went_wrong.trim();
    if (draft.review_lessons.trim())
      payload.review_lessons = draft.review_lessons.trim();

    setErrorFor(null);
    reviewMutation.mutate(
      { id: position.id, payload },
      {
        onSuccess: () => {
          // Drop the draft; the row disappears once the query refetches.
          setDrafts((prev) => {
            const next = { ...prev };
            delete next[position.id];
            return next;
          });
        },
        onError: () => setErrorFor(position.id),
      }
    );
  };

  const shell = (children: React.ReactNode) => (
    <div className="p-5 rounded-xl border border-obsidian-border bg-obsidian-card">
      <div className="flex items-center space-x-2 mb-5">
        <Inbox className="h-4 w-4 text-obsidian-muted" />
        <h2 className="text-sm font-semibold tracking-wide text-slate-200">
          TRADE INBOX
        </h2>
      </div>
      {children}
    </div>
  );

  // --- Loading -------------------------------------------------------------
  if (positionsQuery.isPending) {
    return shell(
      <div className="flex items-center justify-center py-12 text-obsidian-muted">
        <Loader2 className="h-5 w-5 animate-spin mr-2" />
        <span className="text-sm">Loading positions…</span>
      </div>
    );
  }

  // --- Fetch error ---------------------------------------------------------
  if (positionsQuery.isError) {
    return shell(
      <div className="flex items-center justify-center py-12 text-loss">
        <AlertCircle className="h-5 w-5 mr-2" />
        <span className="text-sm">
          {positionsQuery.error instanceof Error
            ? positionsQuery.error.message
            : 'Failed to load positions.'}
        </span>
      </div>
    );
  }

  const positions = positionsQuery.data ?? [];

  // --- Empty ---------------------------------------------------------------
  if (positions.length === 0) {
    return shell(
      <div className="flex flex-col items-center justify-center py-12 text-center">
        <div className="h-12 w-12 rounded-full bg-win/10 border border-win/30 flex items-center justify-center mb-3">
          <CheckCircle2 className="h-6 w-6 text-win" />
        </div>
        <p className="text-sm font-medium text-slate-200">All trades reviewed!</p>
        <p className="text-xs text-obsidian-muted mt-1">
          Nothing is waiting in the queue.
        </p>
      </div>
    );
  }

  const strategies = strategiesQuery.data ?? [];

  // --- Queue ---------------------------------------------------------------
  return shell(
    <div className="space-y-3">
      <p className="text-xs text-obsidian-muted">
        {positions.length} position{positions.length === 1 ? '' : 's'} awaiting review
      </p>

      {positions.map((position) => {
        const draft = draftFor(position.id);
        const isWin = position.realized_pnl >= 0;
        // Disable only the card being submitted, not the whole queue.
        const isSubmitting =
          reviewMutation.isPending &&
          reviewMutation.variables?.id === position.id;

        return (
          <div
            key={position.id}
            className="rounded-lg border border-obsidian-border bg-obsidian-bg/50 p-4"
          >
            {/* Summary row */}
            <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
              <div className="flex items-center space-x-3">
                <div
                  className={`p-1.5 rounded-lg ${
                    isWin ? 'bg-win/10 text-win' : 'bg-loss/10 text-loss'
                  }`}
                >
                  {isWin ? (
                    <ArrowUpRight className="h-4 w-4" />
                  ) : (
                    <ArrowDownRight className="h-4 w-4" />
                  )}
                </div>
                <div>
                  <div className="flex items-center space-x-2">
                    <span className="font-semibold text-slate-100">
                      {position.symbol}
                    </span>
                    <span className="text-[10px] uppercase font-mono px-1.5 py-0.5 rounded bg-slate-800 text-slate-300 border border-obsidian-border">
                      {position.style}
                    </span>
                  </div>
                  <p className="text-[11px] text-obsidian-muted font-mono mt-0.5">
                    {position.quantity} @ {position.entry_price} →{' '}
                    {position.exit_price} · {formatDateTime(position.entry_time)}
                  </p>
                  {/* Entry/exit above are quantity-weighted, so the underlying
                      scale-ins and scale-outs need a way to be seen. */}
                  <button
                    type="button"
                    onClick={() =>
                      setExpandedFills((current) =>
                        current === position.id ? null : position.id
                      )
                    }
                    className="mt-1 inline-flex items-center space-x-1 text-[10px] uppercase tracking-wider text-obsidian-muted hover:text-slate-300 transition-colors"
                    aria-expanded={expandedFills === position.id}
                  >
                    <ChevronRight
                      className={`h-3 w-3 transition-transform ${
                        expandedFills === position.id ? 'rotate-90' : ''
                      }`}
                    />
                    <span>Executions</span>
                  </button>
                </div>
              </div>

              <span
                className={`text-lg font-bold font-mono ${
                  isWin ? 'text-win' : 'text-loss'
                }`}
              >
                {currency(position.realized_pnl)}
              </span>
            </div>

            <PositionFills
              positionId={position.id}
              open={expandedFills === position.id}
            />

            {/* Review checklist */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className="space-y-3">
                <label className="block">
                  <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
                    Strategy
                  </span>
                  <select
                    value={draft.strategy_id}
                    onChange={(e) =>
                      patchDraft(position.id, { strategy_id: e.target.value })
                    }
                    disabled={isSubmitting || strategiesQuery.isPending}
                    className="mt-1 w-full rounded-lg bg-obsidian-card border border-obsidian-border px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-slate-600 disabled:opacity-50"
                  >
                    <option value="">
                      {strategiesQuery.isPending
                        ? 'Loading…'
                        : '— Select strategy —'}
                    </option>
                    {strategies.map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.name}
                      </option>
                    ))}
                  </select>
                </label>

                <div>
                  <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
                    Grade
                  </span>
                  <div className="mt-1 flex gap-1.5">
                    {GRADES.map((g) => {
                      const active = draft.trade_grade === g;
                      return (
                        <button
                          key={g}
                          type="button"
                          disabled={isSubmitting}
                          onClick={() =>
                            patchDraft(position.id, {
                              // Click the active grade again to clear it.
                              trade_grade: active ? '' : g,
                            })
                          }
                          className={`h-7 w-7 rounded-md border text-xs font-semibold transition-colors disabled:opacity-50 ${
                            active
                              ? 'border-win/40 bg-win/10 text-win'
                              : 'border-obsidian-border bg-obsidian-card text-obsidian-muted hover:text-slate-200 hover:border-slate-600'
                          }`}
                        >
                          {g}
                        </button>
                      );
                    })}
                  </div>
                </div>
              </div>

              <div className="space-y-2">
                <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
                  Discipline
                </span>
                {(
                  [
                    ['tag_hard_sl', 'Hard stop-loss set'],
                    ['tag_retest', 'Waited for retest'],
                    ['tag_plan_compliant', 'Followed the plan'],
                  ] as const
                ).map(([field, label]) => (
                  <label
                    key={field}
                    className="flex items-center space-x-2 cursor-pointer select-none"
                  >
                    <input
                      type="checkbox"
                      checked={draft[field]}
                      disabled={isSubmitting}
                      onChange={(e) =>
                        patchDraft(position.id, { [field]: e.target.checked })
                      }
                      className="h-3.5 w-3.5 rounded border-obsidian-border bg-obsidian-card accent-win disabled:opacity-50"
                    />
                    <span className="text-xs text-slate-300">{label}</span>
                  </label>
                ))}
              </div>
            </div>

            {/* The post-mortem. Three questions rather than one notes box:
                asked together, a single field reliably becomes a list of
                mistakes, and what worked never gets written down. */}
            <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-3">
              {(
                [
                  ['review_went_well', 'What went well', 'Executed the entry trigger exactly…'],
                  ['review_went_wrong', 'What went wrong', 'Sized up after two losses…'],
                  ['review_lessons', 'What to learn', 'Wait for the retest before adding…'],
                ] as const
              ).map(([field, label, placeholder]) => (
                <label key={field} className="block">
                  <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
                    {label}
                  </span>
                  <textarea
                    value={draft[field]}
                    disabled={isSubmitting}
                    onChange={(e) =>
                      patchDraft(position.id, { [field]: e.target.value })
                    }
                    rows={3}
                    placeholder={placeholder}
                    className="mt-1 w-full resize-y rounded-lg border border-obsidian-border bg-obsidian-card px-3 py-2 text-xs text-slate-200 placeholder:text-obsidian-muted focus:border-slate-600 focus:outline-none disabled:opacity-50"
                  />
                </label>
              ))}
            </div>

            {errorFor === position.id && reviewMutation.error && (
              <div className="mt-3 flex items-center text-xs text-loss">
                <AlertCircle className="h-3.5 w-3.5 mr-1.5" />
                {reviewMutation.error.message}
              </div>
            )}

            <div className="mt-4 flex justify-end">
              <button
                type="button"
                onClick={() => handleSubmit(position)}
                disabled={isSubmitting}
                className="inline-flex items-center gap-2 rounded-lg border border-win-border bg-win-glow px-3.5 py-2 text-xs font-medium text-win transition-all hover:bg-win/20 disabled:opacity-60 disabled:cursor-not-allowed"
              >
                {isSubmitting ? (
                  <>
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    Saving…
                  </>
                ) : (
                  <>
                    <CheckCircle2 className="h-3.5 w-3.5" />
                    Complete Review
                  </>
                )}
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default TradeInboxQueue;

'use client';

import React, { useState } from 'react';
import {
  AlertCircle,
  ArrowDownRight,
  ArrowUpRight,
  CheckCircle2,
  ChevronRight,
  EyeOff,
  Inbox,
  Loader2,
  Plus,
  Trash2,
  X,
} from 'lucide-react';

import {
  useCreateDiscipline,
  useDeleteDiscipline,
  useDeletePosition,
  useDisciplines,
  useDismissPosition,
  usePendingPositions,
  usePositionDeleteImpact,
  useReviewPosition,
  useStrategies,
} from '@/hooks/useTradeInbox';
import { PositionFills } from '@/components/PositionFills';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { usePendingActions } from '@/components/PendingActionProvider';
import type {
  Position,
  PositionDeleteResult,
  PositionReviewPayload,
} from '@/types/api';

const GRADES = ['A', 'B', 'C', 'D', 'F'] as const;

/** Draft checklist state for one card, before it is submitted. */
interface ReviewDraft {
  strategy_id: string;
  tag_hard_sl: boolean;
  tag_retest: boolean;
  tag_plan_compliant: boolean;
  disciplines_checked: Record<string, boolean>;
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
  disciplines_checked: {},
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
  const disciplinesQuery = useDisciplines();
  const createDisciplineMutation = useCreateDiscipline();
  const deleteDisciplineMutation = useDeleteDiscipline();
  const reviewMutation = useReviewPosition();
  const deletePositionMutation = useDeletePosition();
  const dismissMutation = useDismissPosition();
  // Outcome of a dismiss or delete. Both reach beyond the card that was
  // clicked -- delete rebuilds the ticker's round trips -- so what happened is
  // reported rather than left to be inferred from a changed total.
  const [actionNotice, setActionNotice] = useState<string | null>(null);
  // Which round trip is being confirmed for deletion, if any. Holds the whole
  // position rather than an id so the dialog can name the P&L at stake.
  const [confirmingDelete, setConfirmingDelete] = useState<Position | null>(null);
  // Asked while the dialog is opening, not after the user answers. Deleting a
  // round trip deletes its executions, and an execution can belong to the round
  // trip beside it too, so the prompt has to be able to name what else goes.
  const impactQuery = usePositionDeleteImpact(confirmingDelete?.id ?? null);
  const sharedRoundTrips = impactQuery.data?.shared_round_trips ?? [];
  const { schedule, isPending } = usePendingActions();

  // Drafts are keyed by position id so each card edits independently.
  const [drafts, setDrafts] = useState<Record<string, ReviewDraft>>({});
  // Which card the last mutation error belongs to.
  const [errorFor, setErrorFor] = useState<string | null>(null);
  // Only one execution drill-down open at a time, so the queue stays scannable.
  const [expandedFills, setExpandedFills] = useState<string | null>(null);

  // Manage discipline rules popover state
  const [isManagingRules, setIsManagingRules] = useState(false);
  const [newRuleName, setNewRuleName] = useState('');
  const [ruleError, setRuleError] = useState<string | null>(null);

  const draftFor = (id: string): ReviewDraft => drafts[id] ?? emptyDraft;

  const patchDraft = (id: string, patch: Partial<ReviewDraft>) => {
    setDrafts((prev) => ({
      ...prev,
      [id]: { ...(prev[id] ?? emptyDraft), ...patch },
    }));
  };

  const handleSubmit = (position: Position) => {
    const draft = draftFor(position.id);
    const checked = draft.disciplines_checked ?? {};
    const rules = disciplinesQuery.data ?? [];

    // Every rule on screen gets an answer, not just the ticked ones: submitting
    // the checklist means the user considered all of it, so an unticked box is
    // a real "did not follow" rather than "not yet reviewed". The backend keeps
    // those distinct, and a rule left out entirely stays unanswered.
    const disciplines: Record<string, boolean> = {};
    for (const rule of rules) {
      const saved = position.disciplines?.find((a) => a.discipline_id === rule.id);
      disciplines[rule.id] = Boolean(checked[rule.id] ?? saved?.followed ?? false);
    }

    // Send only what the user actually set; the backend applies just the keys
    // present, so omitting a field leaves it untouched rather than nulling it.
    const payload: PositionReviewPayload = {};
    if (rules.length > 0) payload.disciplines = disciplines;

    // Mirror the three original rules onto their legacy columns. Migration 014
    // supersedes but does not drop them, so anything still reading tag_* stays
    // correct rather than silently freezing at its pre-014 value.
    const byName = (name: string) => {
      const rule = rules.find((r) => r.name === name);
      return rule ? disciplines[rule.id] : undefined;
    };
    const hardSl = byName('Hard stop-loss set');
    const retest = byName('Waited for retest');
    const plan = byName('Followed the plan');
    if (hardSl !== undefined) payload.tag_hard_sl = hardSl;
    if (retest !== undefined) payload.tag_retest = retest;
    if (plan !== undefined) payload.tag_plan_compliant = plan;
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
      {actionNotice && (
        <div className="mb-4 flex items-start gap-2 rounded-lg border border-obsidian-border bg-obsidian-bg/60 px-3 py-2 text-[11px] text-slate-300">
          <AlertCircle className="mt-px h-3.5 w-3.5 shrink-0 text-obsidian-muted" />
          <span className="flex-1">{actionNotice}</span>
          <button
            type="button"
            onClick={() => setActionNotice(null)}
            aria-label="Dismiss message"
            className="text-obsidian-muted hover:text-slate-200"
          >
            <X className="h-3 w-3" />
          </button>
        </div>
      )}
      {children}

      {/* Mounted in the shell so it survives the queue emptying: hiding the
          last card during its undo window switches this component to the empty
          state, and a dialog rendered beside the list would vanish with it. */}
      <ConfirmDialog
        open={confirmingDelete !== null}
        title={`Delete this ${confirmingDelete?.symbol ?? ''} round trip?`}
        confirmLabel={sharedRoundTrips.length > 0 ? 'Delete both' : 'Delete round trip'}
        cancelLabel="Keep it"
        // Nothing is confirmable until the preflight has answered. The whole
        // point of asking the server first is that the question on screen might
        // be the wrong one -- deleting this round trip can delete another.
        confirmDisabled={impactQuery.isPending}
        onCancel={() => setConfirmingDelete(null)}
        onConfirm={() => {
          const position = confirmingDelete;
          const alsoRemoved = sharedRoundTrips.length;
          setConfirmingDelete(null);
          if (!position) return;
          setActionNotice(null);
          schedule({
            id: `position:${position.id}`,
            label:
              alsoRemoved > 0
                ? `${position.symbol} round trip deleted, with ${alsoRemoved} more`
                : `${position.symbol} round trip deleted`,
            detail:
              position.realized_pnl === null
                ? undefined
                : `P&L of ${
                    position.realized_pnl >= 0 ? '+' : ''
                  }$${position.realized_pnl.toFixed(2)} leaves your analytics`,
            commit: () =>
              deletePositionMutation.mutateAsync({
                id: position.id,
                reason: 'Deleted from the Trade Inbox',
                // Only ever true after the dialog above named what else goes.
                // The API returns 409 without it, which is the guard for any
                // client that did not ask first.
                includeShared: alsoRemoved > 0,
              }),
            onCommitted: (result) => {
              const r = result as PositionDeleteResult;
              setActionNotice(
                `${r.ticker}: removed, ${r.executions_deleted} execution(s) deleted` +
                  (r.positions_removed > 0
                    ? `, ${r.positions_removed} other round trip(s) removed with it` +
                      (r.reviews_discarded > 0
                        ? ` and ${r.reviews_discarded} review(s) lost`
                        : '')
                    : '') +
                  (r.suppressed_from_future_syncs > 0
                    ? `, ${r.suppressed_from_future_syncs} suppressed from future syncs.`
                    : '.')
              );
            },
            onError: (err) => setActionNotice(err.message),
          });
        }}
      >
        <p>
          This removes the underlying executions and rebuilds{' '}
          <span className="text-slate-100">{confirmingDelete?.symbol}</span>&apos;s
          round trips.
        </p>

        {/* The reason this dialog waits on the server. One execution can belong
            to two round trips -- an oversell closes the long and opens the
            short with the same fill -- so deleting this one's executions can
            destroy the round trip beside it, and its review with it. Naming
            them here is the difference between confirming and being told
            afterwards. */}
        {impactQuery.isPending && (
          <p className="text-obsidian-muted">Checking what else this would remove…</p>
        )}
        {sharedRoundTrips.length > 0 && (
          <div className="rounded-lg border border-loss/30 bg-loss/5 px-2.5 py-2">
            <p className="font-semibold text-loss">
              {sharedRoundTrips.length} other round trip
              {sharedRoundTrips.length === 1 ? '' : 's'} will be deleted too.
            </p>
            <p className="mt-1 text-obsidian-muted">
              {sharedRoundTrips.length === 1 ? 'It shares' : 'They share'} an
              execution with this one — a fill that closed one position and
              opened the next — so it cannot be removed alone.
            </p>
            <ul className="mt-1.5 space-y-0.5">
              {sharedRoundTrips.map((rt) => (
                <li key={rt.position_id} className="font-mono text-[11px] text-slate-300">
                  {rt.symbol} {Number(rt.quantity)} @{' '}
                  {new Date(rt.entry_time).toLocaleDateString()} ·{' '}
                  <span className={rt.realized_pnl >= 0 ? 'text-win' : 'text-loss'}>
                    {rt.realized_pnl >= 0 ? '+' : ''}${rt.realized_pnl.toFixed(2)}
                  </span>
                  {rt.has_review && (
                    <span className="text-amber-300"> · reviewed</span>
                  )}
                </li>
              ))}
            </ul>
            {impactQuery.data && impactQuery.data.reviews_at_risk > 0 && (
              <p className="mt-1.5 text-amber-200/90">
                {impactQuery.data.reviews_at_risk} review
                {impactQuery.data.reviews_at_risk === 1 ? '' : 's'} cannot be
                rebuilt. Everything else re-matches from the fills; this does not.
              </p>
            )}
            <p className="mt-1.5 text-obsidian-muted">
              To keep {sharedRoundTrips.length === 1 ? 'it' : 'them'}, cancel and
              delete the individual fills from the Journal instead.
            </p>
          </div>
        )}
        {impactQuery.isError && (
          <p className="text-amber-300">
            Could not check for shared executions ({impactQuery.error.message}).
            Deleting may remove more than this round trip.
          </p>
        )}
        {confirmingDelete?.realized_pnl != null && (
          <p>
            Its P&amp;L of{' '}
            <span
              className={
                confirmingDelete.realized_pnl >= 0 ? 'text-win' : 'text-loss'
              }
            >
              {confirmingDelete.realized_pnl >= 0 ? '+' : ''}$
              {confirmingDelete.realized_pnl.toFixed(2)}
            </span>{' '}
            leaves your analytics.
          </p>
        )}
        <p>
          Broker fills are also suppressed, so the next sync cannot re-add them.
        </p>
        <p className="text-obsidian-muted">
          Use this only if the trade never happened. To keep the P&amp;L and just
          clear the queue, choose <span className="text-slate-300">Dismiss</span>{' '}
          instead.
        </p>
      </ConfirmDialog>
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

  // Rows inside their undo window are hidden here rather than after the
  // request lands. Without that the card would sit there looking undeleted for
  // ten seconds, and the obvious response is to press Delete again.
  const positions = (positionsQuery.data ?? []).filter(
    (p) => !isPending(`position:${p.id}`)
  );

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

              <div className="space-y-2 relative">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
                    Discipline
                  </span>
                  <button
                    type="button"
                    onClick={() => {
                      setIsManagingRules((prev) => !prev);
                      setRuleError(null);
                    }}
                    className="text-[10px] text-emerald-400 hover:underline flex items-center gap-1 font-medium"
                  >
                    <Plus className="h-3 w-3" />
                    <span>Manage Rules</span>
                  </button>
                </div>

                {/* Manage Rules Popover */}
                {isManagingRules && (
                  <div className="absolute right-0 top-6 z-20 w-72 rounded-lg border border-obsidian-border bg-obsidian-card p-3 shadow-xl space-y-3 text-xs">
                    <div className="flex items-center justify-between border-b border-obsidian-border pb-2">
                      <span className="font-semibold text-slate-200">Discipline Rules</span>
                      <button
                        type="button"
                        onClick={() => setIsManagingRules(false)}
                        className="text-obsidian-muted hover:text-slate-200"
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </div>

                    <div className="max-h-40 overflow-y-auto space-y-1.5 pr-1">
                      {(disciplinesQuery.data ?? []).map((d) => (
                        <div key={d.id} className="flex items-center justify-between bg-obsidian-bg/60 px-2 py-1 rounded border border-obsidian-border/50 text-slate-300">
                          <span className="truncate pr-2">{d.name}</span>
                          <button
                            type="button"
                            onClick={() => deleteDisciplineMutation.mutate(d.id)}
                            disabled={deleteDisciplineMutation.isPending}
                            className="text-loss hover:opacity-80 p-0.5"
                            title="Delete rule"
                          >
                            <Trash2 className="h-3 w-3" />
                          </button>
                        </div>
                      ))}
                    </div>

                    {ruleError && (
                      <p className="text-[10px] text-loss">{ruleError}</p>
                    )}

                    <div className="flex gap-1.5 pt-1 border-t border-obsidian-border">
                      <input
                        type="text"
                        value={newRuleName}
                        onChange={(e) => setNewRuleName(e.target.value)}
                        placeholder="New rule name…"
                        className="flex-1 rounded bg-obsidian-bg border border-obsidian-border px-2 py-1 text-xs text-slate-200 focus:outline-none focus:border-slate-600"
                      />
                      <button
                        type="button"
                        onClick={() => {
                          const name = newRuleName.trim();
                          if (!name) return;
                          setRuleError(null);
                          createDisciplineMutation.mutate(
                            { name },
                            {
                              onSuccess: () => setNewRuleName(''),
                              onError: (err) => setRuleError(err.message),
                            }
                          );
                        }}
                        disabled={createDisciplineMutation.isPending || !newRuleName.trim()}
                        className="rounded bg-win/20 border border-win/40 px-2 py-1 text-win font-medium hover:bg-win/30 disabled:opacity-50"
                      >
                        Add
                      </button>
                    </div>
                  </div>
                )}

                {disciplinesQuery.isPending ? (
                  <div className="text-xs text-obsidian-muted flex items-center gap-1.5 py-1">
                    <Loader2 className="h-3 w-3 animate-spin" />
                    <span>Loading rules…</span>
                  </div>
                ) : (disciplinesQuery.data ?? []).length === 0 ? (
                  <p className="text-xs text-obsidian-muted py-1">No discipline rules defined.</p>
                ) : (
                  (disciplinesQuery.data ?? []).map((d) => {
                    // Keyed by id, never by name. Name-keying meant a rule the
                    // user added had no column to land in and was dropped on
                    // save, and renaming a default rule silently detached its
                    // history. Any previously saved answer wins over the draft
                    // default so an unreviewed rule starts unticked.
                    const saved = position.disciplines?.find(
                      (a) => a.discipline_id === d.id
                    );
                    const isChecked = Boolean(
                      draft.disciplines_checked?.[d.id] ?? saved?.followed ?? false
                    );
                    return (
                      <label
                        key={d.id}
                        className="flex items-center space-x-2 cursor-pointer select-none"
                      >
                        <input
                          type="checkbox"
                          checked={isChecked}
                          disabled={isSubmitting}
                          onChange={(e) =>
                            patchDraft(position.id, {
                              disciplines_checked: {
                                ...(draft.disciplines_checked ?? {}),
                                [d.id]: e.target.checked,
                              },
                            })
                          }
                          className="h-3.5 w-3.5 rounded border-obsidian-border bg-obsidian-card accent-win disabled:opacity-50"
                        />
                        <span className="text-xs text-slate-300">{d.name}</span>
                      </label>
                    );
                  })
                )}
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

            <div className="mt-4 flex flex-wrap items-center justify-end gap-2">
              {/* Two ways out that are not a review, and they are not the
                  same. Dismiss is for a real trade you have nothing to write
                  about -- the P&L stays in every analytic. Delete is for a
                  round trip that never happened, such as one a duplicate sync
                  invented, and removes the executions underneath it. */}
              <button
                type="button"
                onClick={() => setConfirmingDelete(position)}
                disabled={isSubmitting || deletePositionMutation.isPending || dismissMutation.isPending}
                className="inline-flex items-center gap-1.5 rounded-lg border border-loss/30 bg-loss/10 px-3 py-2 text-xs font-medium text-loss transition-colors hover:bg-loss/20 disabled:opacity-60"
              >
                <Trash2 className="h-3.5 w-3.5" />
                Delete Trade
              </button>

              <button
                type="button"
                onClick={() =>
                  dismissMutation.mutate(position.id, {
                    onSuccess: () =>
                      setActionNotice(
                        `${position.symbol} dismissed — it keeps its P&L and leaves the queue.`
                      ),
                    onError: (err) => setActionNotice(err.message),
                  })
                }
                disabled={isSubmitting || dismissMutation.isPending || deletePositionMutation.isPending}
                className="inline-flex items-center gap-1.5 rounded-lg border border-obsidian-border bg-obsidian-bg px-3 py-2 text-xs font-medium text-obsidian-muted transition-colors hover:text-slate-200 hover:border-slate-600 disabled:opacity-60"
                title="Leave the queue without writing a review. The trade and its P&L are kept."
              >
                {dismissMutation.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <EyeOff className="h-3.5 w-3.5" />
                )}
                Dismiss
              </button>

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

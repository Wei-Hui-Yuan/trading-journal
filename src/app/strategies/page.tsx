'use client';

import React, { useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  BookOpen,
  Check,
  ListChecks,
  Loader2,
  Plus,
  Target,
  Trash2,
  TrendingUp,
} from 'lucide-react';

import {
  useCreateDiscipline,
  useCreateStrategy,
  useDeleteDiscipline,
  useDeleteStrategy,
  useDisciplines,
  useStrategies,
  useUpdateStrategy,
} from '@/hooks/useTradeInbox';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { usePendingActions } from '@/components/PendingActionProvider';
import type { Strategy, StrategyDeleteResult, StrategyUsage } from '@/types/api';

/**
 * How many rows point at a strategy.
 *
 * Trades and round trips are different grains of the same history, so they are
 * summed here only to answer "is anything attached at all" — the dialog
 * reports them separately, because "76 trades" and "1 round trip" are not
 * interchangeable facts.
 */
const usageTotal = (u?: StrategyUsage): number =>
  u ? u.trades + u.positions + u.plans : 0;

/** Editable shape of one strategy; mirrors the API fields the form owns. */
interface StrategyDraft {
  name: string;
  description: string;
  method: string;
  entry_criteria: string;
  exit_criteria: string;
}

const BLANK_DRAFT: StrategyDraft = {
  name: '',
  description: '',
  method: '',
  entry_criteria: '',
  exit_criteria: '',
};

const toDraft = (s: Strategy): StrategyDraft => ({
  name: s.name,
  description: s.description ?? '',
  method: s.method ?? '',
  entry_criteria: s.entry_criteria ?? '',
  exit_criteria: s.exit_criteria ?? '',
});

/** Sentinel id for the unsaved "new strategy" row. */
const NEW_ID = '__new__';

/**
 * Checklist items scoped to one strategy (migration 032) -- separate from
 * `entry_criteria`/`exit_criteria` above, which stay free text. Those explain
 * the setup; these are the literal yes/no items the review checklist shows
 * on a trade tagged with this strategy, alongside the general rules that
 * apply to every trade regardless of setup.
 *
 * Own component so its own discipline-mutation loading/error state does not
 * get tangled with the surrounding strategy-editor form's.
 */
function StrategyChecklist({ strategyId }: { strategyId: string }) {
  const disciplinesQuery = useDisciplines();
  const createMutation = useCreateDiscipline();
  const deleteMutation = useDeleteDiscipline();
  const [newItem, setNewItem] = useState('');
  const [error, setError] = useState<string | null>(null);

  const items = (disciplinesQuery.data ?? []).filter(
    (d) => d.strategy_id === strategyId
  );

  const handleAdd = () => {
    const name = newItem.trim();
    if (!name) return;
    setError(null);
    createMutation.mutate(
      { name, strategy_id: strategyId },
      {
        onSuccess: () => setNewItem(''),
        onError: (err) => setError(err.message),
      }
    );
  };

  return (
    <div>
      <span className="inline-flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-obsidian-muted">
        <ListChecks className="h-3 w-3 text-slate-400" />
        Review Checklist
      </span>
      <p className="mt-1 text-[10px] text-obsidian-muted">
        Shown alongside the general rules when reviewing a trade tagged with
        this strategy. Separate from the rules above, which are notes for
        yourself rather than something checked off per trade.
      </p>

      <div className="mt-2 space-y-1.5">
        {disciplinesQuery.isPending ? (
          <div className="flex items-center gap-1.5 text-xs text-obsidian-muted">
            <Loader2 className="h-3 w-3 animate-spin" />
            <span>Loading…</span>
          </div>
        ) : items.length === 0 ? (
          <p className="text-xs text-obsidian-muted">No checklist items yet.</p>
        ) : (
          items.map((item) => (
            <div
              key={item.id}
              className="flex items-center justify-between rounded border border-obsidian-border/50 bg-obsidian-bg px-2.5 py-1.5 text-xs text-slate-300"
            >
              <span className="truncate pr-2">{item.name}</span>
              <button
                type="button"
                onClick={() => deleteMutation.mutate(item.id)}
                disabled={deleteMutation.isPending}
                title="Remove item"
                className="p-0.5 text-obsidian-muted hover:text-loss disabled:opacity-50"
              >
                <Trash2 className="h-3 w-3" />
              </button>
            </div>
          ))
        )}
      </div>

      {error && <p className="mt-2 text-[10px] text-loss">{error}</p>}

      <div className="mt-2 flex gap-1.5">
        <input
          type="text"
          value={newItem}
          onChange={(e) => setNewItem(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleAdd()}
          placeholder="e.g. Price reclaimed prior day high"
          disabled={createMutation.isPending}
          className="flex-1 rounded-lg bg-obsidian-bg border border-obsidian-border px-2.5 py-1.5 text-xs text-slate-200 placeholder:text-obsidian-muted focus:outline-none focus:border-slate-600 disabled:opacity-50"
        />
        <button
          type="button"
          onClick={handleAdd}
          disabled={createMutation.isPending || !newItem.trim()}
          className="inline-flex items-center gap-1 rounded-lg border border-win-border bg-win-glow px-2.5 py-1.5 text-xs font-medium text-win hover:bg-win/20 disabled:opacity-50"
        >
          <Plus className="h-3 w-3" />
          Add
        </button>
      </div>
    </div>
  );
}

export default function StrategiesPage() {
  const strategiesQuery = useStrategies();
  const createMutation = useCreateStrategy();
  const updateMutation = useUpdateStrategy();

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draft, setDraft] = useState<StrategyDraft>(BLANK_DRAFT);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  const deleteMutation = useDeleteStrategy();
  const { schedule, isPending } = usePendingActions();
  const [confirmingDelete, setConfirmingDelete] = useState<Strategy | null>(null);
  // '' until a reassignment target is chosen. The dialog will not confirm
  // without one whenever the strategy has history attached.
  const [reassignTo, setReassignTo] = useState('');
  const [deleteNotice, setDeleteNotice] = useState<string | null>(null);

  const strategies = useMemo(
    // Hidden while its undo window runs, so the row does not sit in the list
    // looking undeleted — and cannot be picked as its own reassignment target.
    () => (strategiesQuery.data ?? []).filter((s) => !isPending(`strategy:${s.id}`)),
    [strategiesQuery.data, isPending]
  );

  // Select the first strategy once the list arrives, so the editor is never
  // stranded on an empty pane when data exists.
  useEffect(() => {
    if (selectedId === null && strategies.length > 0) {
      setSelectedId(strategies[0].id);
      setDraft(toDraft(strategies[0]));
    }
  }, [strategies, selectedId]);

  const isCreating = selectedId === NEW_ID;
  const selected = strategies.find((s) => s.id === selectedId) ?? null;
  const isSaving = createMutation.isPending || updateMutation.isPending;

  const selectStrategy = (s: Strategy) => {
    setSelectedId(s.id);
    setDraft(toDraft(s));
    setFormError(null);
    setSavedAt(null);
  };

  const startCreate = () => {
    setSelectedId(NEW_ID);
    setDraft(BLANK_DRAFT);
    setFormError(null);
    setSavedAt(null);
  };

  const patch = (field: keyof StrategyDraft, value: string) => {
    setDraft((prev) => ({ ...prev, [field]: value }));
    setSavedAt(null);
  };

  const handleSave = () => {
    const name = draft.name.trim();
    if (!name) {
      setFormError('Strategy name is required.');
      return;
    }
    setFormError(null);

    const payload = {
      name,
      description: draft.description,
      method: draft.method,
      entry_criteria: draft.entry_criteria,
      exit_criteria: draft.exit_criteria,
    };

    if (isCreating) {
      createMutation.mutate(payload, {
        onSuccess: (created) => {
          // Jump to the persisted row so subsequent saves PATCH instead of
          // creating a second copy.
          setSelectedId(created.id);
          setDraft(toDraft(created));
          setSavedAt(Date.now());
        },
        onError: (err) => setFormError(err.message),
      });
    } else if (selectedId) {
      updateMutation.mutate(
        { id: selectedId, payload },
        {
          onSuccess: (updated) => {
            setDraft(toDraft(updated));
            setSavedAt(Date.now());
          },
          onError: (err) => setFormError(err.message),
        }
      );
    }
  };

  const fieldClass =
    'w-full rounded-lg bg-obsidian-bg border border-obsidian-border px-3 py-2 text-sm text-slate-200 ' +
    'placeholder:text-obsidian-muted focus:outline-none focus:border-slate-600 transition-colors ' +
    'disabled:opacity-50';

  return (
    <div className="min-h-screen bg-obsidian-bg text-slate-100 flex flex-col font-sans">
      {/* Page header */}
      <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <div className="grid grid-cols-1 lg:grid-cols-[300px_1fr] gap-6">

          {/* ---- Left pane: strategy list ---- */}
          <aside className="rounded-xl border border-obsidian-border bg-obsidian-card p-4 h-fit">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-sm font-semibold tracking-wide text-slate-200">
                STRATEGIES
              </h2>
              {!strategiesQuery.isPending && (
                <span className="text-[10px] font-mono text-obsidian-muted">
                  {strategies.length}
                </span>
              )}
            </div>

            <button
              type="button"
              onClick={startCreate}
              className="w-full inline-flex items-center justify-center gap-2 rounded-lg border border-win-border bg-win-glow px-3 py-2 text-xs font-medium text-win hover:bg-win/20 transition-colors mb-4"
            >
              <Plus className="h-3.5 w-3.5" />
              Create New Strategy
            </button>

            {strategiesQuery.isPending && (
              <div className="flex items-center justify-center py-8 text-obsidian-muted">
                <Loader2 className="h-4 w-4 animate-spin mr-2" />
                <span className="text-xs">Loading…</span>
              </div>
            )}

            {strategiesQuery.isError && (
              <div className="flex items-start py-4 text-loss text-xs">
                <AlertCircle className="h-4 w-4 mr-1.5 shrink-0" />
                <span>
                  {strategiesQuery.error instanceof Error
                    ? strategiesQuery.error.message
                    : 'Failed to load strategies.'}
                </span>
              </div>
            )}

            {!strategiesQuery.isPending && strategies.length === 0 && !isCreating && (
              <p className="text-xs text-obsidian-muted py-4 text-center">
                No strategies yet. Create your first one.
              </p>
            )}

            <ul className="space-y-1.5">
              {isCreating && (
                <li>
                  <div className="w-full rounded-lg border border-win/40 bg-win/10 px-3 py-2 text-left">
                    <span className="text-xs font-medium text-win">
                      {draft.name.trim() || 'New strategy'}
                    </span>
                    <p className="text-[10px] text-obsidian-muted mt-0.5">Unsaved</p>
                  </div>
                </li>
              )}

              {strategies.map((s) => {
                const active = s.id === selectedId;
                return (
                  <li key={s.id}>
                    <button
                      type="button"
                      onClick={() => selectStrategy(s)}
                      className={`w-full rounded-lg border px-3 py-2 text-left transition-colors ${
                        active
                          ? 'border-slate-600 bg-obsidian-bg text-slate-100'
                          : 'border-obsidian-border bg-obsidian-bg/40 text-obsidian-muted hover:text-slate-200 hover:border-slate-700'
                      }`}
                    >
                      <span className="text-xs font-medium block truncate">
                        {s.name}
                      </span>
                      <p className="text-[10px] text-obsidian-muted mt-0.5 truncate">
                        {s.method || 'No method set'}
                      </p>
                      {/* Visible before the delete button is ever pressed. A
                          playbook entry with no trades behind it is a
                          different thing from one carrying most of your
                          history, and the list is where that comparison
                          actually happens. */}
                      {s.usage && (
                        <p className="mt-1 text-[10px] font-mono text-obsidian-muted">
                          {s.usage.trades === 0
                            ? 'unused'
                            : `${s.usage.trades} trade${s.usage.trades === 1 ? '' : 's'}`}
                        </p>
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          </aside>

          {/* ---- Right pane: editor ---- */}
          <section className="rounded-xl border border-obsidian-border bg-obsidian-card p-5">
            {selectedId === null && !strategiesQuery.isPending ? (
              <div className="flex flex-col items-center justify-center py-20 text-center">
                <BookOpen className="h-8 w-8 text-obsidian-muted mb-3" />
                <p className="text-sm text-slate-300">
                  Select a strategy to edit
                </p>
                <p className="text-xs text-obsidian-muted mt-1">
                  or create a new one to start your playbook.
                </p>
              </div>
            ) : (
              <>
                <div className="flex items-center justify-between pb-4 border-b border-obsidian-border mb-5">
                  <h2 className="text-base font-semibold text-white">
                    {isCreating ? 'New Strategy' : selected?.name ?? 'Strategy'}
                  </h2>
                  <div className="flex items-center gap-3">
                    {savedAt && (
                      <span className="inline-flex items-center gap-1.5 text-xs text-win">
                        <Check className="h-3.5 w-3.5" />
                        Saved
                      </span>
                    )}
                    {/* Only for a saved strategy: there is nothing to delete
                        on an unsaved draft, and offering it would imply
                        otherwise. */}
                    {!isCreating && selected && (
                      <button
                        type="button"
                        onClick={() => {
                          setConfirmingDelete(selected);
                          setReassignTo('');
                          setDeleteNotice(null);
                        }}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-loss/30 bg-loss/10 px-3 py-1.5 text-xs font-medium text-loss transition-colors hover:bg-loss/20"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                        Delete
                      </button>
                    )}
                  </div>
                </div>

                {deleteNotice && (
                  <div className="mb-4 flex items-start gap-2 rounded-lg border border-obsidian-border bg-obsidian-bg/60 px-3 py-2 text-[11px] text-slate-300">
                    <AlertCircle className="mt-px h-3.5 w-3.5 shrink-0 text-obsidian-muted" />
                    <span className="flex-1">{deleteNotice}</span>
                    <button
                      type="button"
                      onClick={() => setDeleteNotice(null)}
                      aria-label="Dismiss message"
                      className="text-obsidian-muted hover:text-slate-200"
                    >
                      ×
                    </button>
                  </div>
                )}

                <div className="space-y-5">
                  <label className="block">
                    <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
                      Strategy Name
                    </span>
                    <input
                      type="text"
                      value={draft.name}
                      onChange={(e) => patch('name', e.target.value)}
                      disabled={isSaving}
                      placeholder="e.g. Breakout + Retest"
                      className={`mt-1 ${fieldClass}`}
                    />
                  </label>

                  <label className="block">
                    <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
                      Overarching Method
                    </span>
                    <input
                      type="text"
                      value={draft.method}
                      onChange={(e) => patch('method', e.target.value)}
                      disabled={isSaving}
                      placeholder="e.g. Supply/Demand, Momentum Breakout"
                      className={`mt-1 ${fieldClass}`}
                    />
                  </label>

                  <label className="block">
                    <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
                      Description
                    </span>
                    <input
                      type="text"
                      value={draft.description}
                      onChange={(e) => patch('description', e.target.value)}
                      disabled={isSaving}
                      placeholder="One-line summary"
                      className={`mt-1 ${fieldClass}`}
                    />
                  </label>

                  <label className="block">
                    <span className="inline-flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-obsidian-muted">
                      <TrendingUp className="h-3 w-3 text-win" />
                      Entry Rules
                    </span>
                    <textarea
                      value={draft.entry_criteria}
                      onChange={(e) => patch('entry_criteria', e.target.value)}
                      disabled={isSaving}
                      rows={6}
                      placeholder={
                        '- Price reclaims prior day high on volume\n- Wait for retest to hold\n- Enter on first higher low'
                      }
                      className={`mt-1 font-mono text-xs leading-relaxed resize-y ${fieldClass}`}
                    />
                    <span className="text-[10px] text-obsidian-muted">
                      One rule per line.
                    </span>
                  </label>

                  <label className="block">
                    <span className="inline-flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-obsidian-muted">
                      <Target className="h-3 w-3 text-loss" />
                      Exit Rules &amp; Risk
                    </span>
                    <textarea
                      value={draft.exit_criteria}
                      onChange={(e) => patch('exit_criteria', e.target.value)}
                      disabled={isSaving}
                      rows={6}
                      placeholder={
                        '- Hard stop below retest low\n- Risk 1% of account\n- Take 50% at 2R, trail the rest'
                      }
                      className={`mt-1 font-mono text-xs leading-relaxed resize-y ${fieldClass}`}
                    />
                    <span className="text-[10px] text-obsidian-muted">
                      One rule per line.
                    </span>
                  </label>

                  {/* Needs a persisted id to scope items to, so this waits
                      for the strategy to exist -- an unsaved draft has
                      nowhere for a checklist item to point. */}
                  {!isCreating && selected && (
                    <StrategyChecklist strategyId={selected.id} />
                  )}
                </div>

                {formError && (
                  <div className="mt-4 flex items-center text-xs text-loss">
                    <AlertCircle className="h-3.5 w-3.5 mr-1.5" />
                    {formError}
                  </div>
                )}

                <div className="mt-6 flex justify-end">
                  <button
                    type="button"
                    onClick={handleSave}
                    disabled={isSaving}
                    className="inline-flex items-center gap-2 rounded-lg border border-win-border bg-win-glow px-4 py-2 text-xs font-medium text-win hover:bg-win/20 disabled:opacity-60 disabled:cursor-not-allowed transition-colors"
                  >
                    {isSaving ? (
                      <>
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        Saving…
                      </>
                    ) : (
                      <>
                        <Check className="h-3.5 w-3.5" />
                        {isCreating ? 'Create Strategy' : 'Save Changes'}
                      </>
                    )}
                  </button>
                </div>
              </>
            )}
          </section>
        </div>
      </main>

      {/* Deleting a playbook entry cannot lose a trade — the three foreign
          keys are ON DELETE SET NULL. What it can lose is the attribution:
          every trade tagged with it would fall into "Unassigned" in the
          analytics breakdown, with nothing left to say which setup it was.
          So a used strategy demands somewhere for its history to go. */}
      <ConfirmDialog
        open={confirmingDelete !== null}
        title={`Delete "${confirmingDelete?.name ?? ''}"?`}
        confirmLabel={
          usageTotal(confirmingDelete?.usage) > 0
            ? 'Reassign and delete'
            : 'Delete strategy'
        }
        cancelLabel="Keep it"
        confirmDisabled={
          usageTotal(confirmingDelete?.usage) > 0 && reassignTo === ''
        }
        onCancel={() => {
          setConfirmingDelete(null);
          setReassignTo('');
        }}
        onConfirm={() => {
          const doomed = confirmingDelete;
          const target = reassignTo || null;
          setConfirmingDelete(null);
          setReassignTo('');
          if (!doomed) return;

          const targetName =
            strategies.find((s) => s.id === target)?.name ?? null;

          // Land on wherever the history went, so the reassignment is
          // immediately inspectable rather than taken on trust.
          if (selectedId === doomed.id) {
            setSelectedId(target);
            const next = strategies.find((s) => s.id === target);
            if (next) setDraft(toDraft(next));
          }

          schedule({
            id: `strategy:${doomed.id}`,
            label: `"${doomed.name}" deleted`,
            detail: targetName
              ? `history moved to "${targetName}"`
              : 'no trades were attached',
            commit: () =>
              deleteMutation.mutateAsync({ id: doomed.id, reassignTo: target }),
            onCommitted: (result) => {
              const r = result as StrategyDeleteResult;
              setDeleteNotice(
                r.reassigned_to_name
                  ? `"${r.deleted_name}" deleted. ${r.trades_reassigned} trade(s), ` +
                    `${r.positions_reassigned} round trip(s) and ${r.plans_reassigned} plan(s) ` +
                    `now belong to "${r.reassigned_to_name}".`
                  : `"${r.deleted_name}" deleted. Nothing referenced it.`
              );
            },
            onError: (err) => setDeleteNotice(err.message),
          });
        }}
      >
        {usageTotal(confirmingDelete?.usage) === 0 ? (
          <p>
            Nothing references this strategy, so deleting it changes no trade
            and no statistic.
            {(confirmingDelete?.usage?.checklist_items ?? 0) > 0 && (
              <>
                {' '}
                Its {confirmingDelete?.usage?.checklist_items} checklist item
                {confirmingDelete?.usage?.checklist_items === 1 ? '' : 's'} will
                be deleted along with it.
              </>
            )}
          </p>
        ) : (
          <>
            <p>
              <span className="text-slate-100">
                {confirmingDelete?.usage?.trades ?? 0} trade
                {(confirmingDelete?.usage?.trades ?? 0) === 1 ? '' : 's'}
              </span>
              {(confirmingDelete?.usage?.positions ?? 0) > 0 && (
                <>
                  ,{' '}
                  <span className="text-slate-100">
                    {confirmingDelete?.usage?.positions} round trip
                    {confirmingDelete?.usage?.positions === 1 ? '' : 's'}
                  </span>
                </>
              )}
              {(confirmingDelete?.usage?.plans ?? 0) > 0 && (
                <>
                  {' '}and{' '}
                  <span className="text-slate-100">
                    {confirmingDelete?.usage?.plans} open plan
                    {confirmingDelete?.usage?.plans === 1 ? '' : 's'}
                  </span>
                </>
              )}{' '}
              currently use this strategy. They will be reassigned, not deleted.
            </p>

            {/* Unlike trades/positions/plans above, checklist items have
                nowhere sensible to be reassigned TO -- a rule like "waited
                for the gap fill" describes this setup specifically. They are
                deleted, not moved, which is why this is its own sentence
                rather than folded into the reassignment note above. */}
            {(confirmingDelete?.usage?.checklist_items ?? 0) > 0 && (
              <p>
                Its{' '}
                <span className="text-slate-100">
                  {confirmingDelete?.usage?.checklist_items} checklist item
                  {confirmingDelete?.usage?.checklist_items === 1 ? '' : 's'}
                </span>{' '}
                will be deleted, not reassigned.
              </p>
            )}

            <label className="block pt-1">
              <span className="text-[10px] uppercase tracking-wider text-obsidian-muted">
                Reassign them to
              </span>
              <select
                value={reassignTo}
                onChange={(e) => setReassignTo(e.target.value)}
                className="mt-1 w-full rounded-lg border border-obsidian-border bg-obsidian-bg px-3 py-2 text-xs text-slate-200 focus:border-slate-600 focus:outline-none"
              >
                <option value="">— Choose a strategy —</option>
                {strategies
                  .filter((s) => s.id !== confirmingDelete?.id)
                  .map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
              </select>
            </label>

            {strategies.filter((s) => s.id !== confirmingDelete?.id).length ===
              0 && (
              <p className="text-loss">
                This is your only strategy, so there is nowhere to move its
                history. Create another one first.
              </p>
            )}

            <p className="text-obsidian-muted">
              Every figure in the analytics breakdown for this strategy will be
              counted under the one you choose. That cannot be undone once the
              delete goes through.
            </p>
          </>
        )}
      </ConfirmDialog>
    </div>
  );
}

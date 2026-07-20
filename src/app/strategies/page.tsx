'use client';

import React, { useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import {
  AlertCircle,
  ArrowLeft,
  BookOpen,
  Check,
  Loader2,
  Plus,
  Target,
  TrendingUp,
} from 'lucide-react';

import {
  useCreateStrategy,
  useStrategies,
  useUpdateStrategy,
} from '@/hooks/useTradeInbox';
import type { Strategy } from '@/types/api';

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

export default function StrategiesPage() {
  const strategiesQuery = useStrategies();
  const createMutation = useCreateStrategy();
  const updateMutation = useUpdateStrategy();

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draft, setDraft] = useState<StrategyDraft>(BLANK_DRAFT);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  const strategies = useMemo(
    () => strategiesQuery.data ?? [],
    [strategiesQuery.data]
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
      <header className="border-b border-obsidian-border bg-obsidian-card/80 backdrop-blur-md sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <div className="h-9 w-9 rounded-xl bg-gradient-to-br from-win/20 to-emerald-900/40 border border-win/30 flex items-center justify-center">
              <BookOpen className="h-5 w-5 text-win" />
            </div>
            <div>
              <span className="font-bold text-lg tracking-wider text-white">
                STRATEGY PLAYBOOK
              </span>
              <p className="text-xs text-obsidian-muted font-medium">
                Define methods, entry triggers, and exit rules
              </p>
            </div>
          </div>

          <Link
            href="/"
            className="inline-flex items-center gap-2 rounded-lg bg-obsidian-bg border border-obsidian-border px-3 py-2 text-xs font-medium text-obsidian-muted hover:text-slate-100 hover:border-slate-600 transition-colors"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            Dashboard
          </Link>
        </div>
      </header>

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
                  {savedAt && (
                    <span className="inline-flex items-center gap-1.5 text-xs text-win">
                      <Check className="h-3.5 w-3.5" />
                      Saved
                    </span>
                  )}
                </div>

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
    </div>
  );
}

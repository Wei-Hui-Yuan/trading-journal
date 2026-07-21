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
} from 'lucide-react';

import { useAnnotateTrade, useStrategies, useTrades } from '@/hooks/useTradeInbox';
import type { Trade } from '@/types/api';

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

type Filter = 'all' | 'open' | 'closed';

/**
 * The master list: every execution, open or closed.
 *
 * The Trade Inbox and analytics both read `positions`, which only ever holds
 * *closed* round trips. A buy that has not been sold produces no position, so
 * before this view a hand-logged entry was saved correctly and then appeared
 * nowhere at all.
 */
export const TradeLedger: React.FC = () => {
  const { data: trades, isLoading, error } = useTrades();
  const { data: strategies } = useStrategies();
  const annotate = useAnnotateTrade();

  const [expanded, setExpanded] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>('all');
  const [query, setQuery] = useState('');
  // Drafts are per-trade so two rows never share an editing buffer.
  const [drafts, setDrafts] = useState<Record<string, { strategyId: string; thesis: string }>>({});

  const visible = useMemo(() => {
    const term = query.trim().toUpperCase();
    return (trades ?? []).filter((t) => {
      if (filter === 'open' && t.is_matched) return false;
      if (filter === 'closed' && !t.is_matched) return false;
      return !term || t.ticker.includes(term);
    });
  }, [trades, filter, query]);

  const openCount = (trades ?? []).filter((t) => !t.is_matched).length;

  const draftFor = (t: Trade) =>
    drafts[t.id] ?? { strategyId: t.strategy_id ?? '', thesis: t.thesis ?? '' };

  const strategyName = (id: string | null) =>
    id ? (strategies ?? []).find((s) => s.id === id)?.name ?? null : null;

  const save = (t: Trade) => {
    const d = draftFor(t);
    annotate.mutate({
      id: t.id,
      payload: { strategy_id: d.strategyId || null, thesis: d.thesis.trim() || null },
    });
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16 text-sm text-obsidian-muted">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        Loading ledger…
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

  const fieldClass =
    'w-full rounded-lg bg-obsidian-bg border border-obsidian-border px-3 py-2 text-xs text-slate-200 ' +
    'placeholder:text-obsidian-muted focus:outline-none focus:border-slate-600 transition-colors';

  return (
    <div className="space-y-4">
      {/* Controls */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[180px]">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-obsidian-muted" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter by ticker…"
            className={`${fieldClass} pl-8`}
          />
        </div>
        <div className="flex rounded-lg border border-obsidian-border overflow-hidden">
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
          No executions match this filter.
        </p>
      ) : (
        <div className="space-y-2">
          {visible.map((t) => {
            const isBuy = t.direction === 'BUY';
            const isOpen = !t.is_matched;
            const d = draftFor(t);
            const name = strategyName(t.strategy_id);

            return (
              <div
                key={t.id}
                className="rounded-xl border border-obsidian-border bg-obsidian-card"
              >
                <button
                  type="button"
                  onClick={() => setExpanded((c) => (c === t.id ? null : t.id))}
                  className="flex w-full items-center gap-3 px-4 py-3 text-left"
                >
                  <ChevronRight
                    className={`h-3.5 w-3.5 shrink-0 text-obsidian-muted transition-transform ${
                      expanded === t.id ? 'rotate-90' : ''
                    }`}
                  />
                  <div
                    className={`rounded-lg p-1.5 ${
                      isBuy ? 'bg-win/10 text-win' : 'bg-loss/10 text-loss'
                    }`}
                  >
                    {isBuy ? (
                      <ArrowUpRight className="h-3.5 w-3.5" />
                    ) : (
                      <ArrowDownRight className="h-3.5 w-3.5" />
                    )}
                  </div>

                  <span className="w-16 shrink-0 font-semibold text-slate-100">
                    {t.ticker}
                  </span>

                  <span className="w-40 shrink-0 font-mono text-[11px] text-obsidian-muted">
                    {formatQuantity(t.quantity)} @ {t.actual_entry}
                  </span>

                  <span
                    className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wider ${
                      isOpen
                        ? 'bg-amber-500/10 text-amber-300'
                        : 'bg-slate-800 text-slate-400'
                    }`}
                  >
                    {isOpen ? 'Open' : 'Closed'}
                  </span>

                  {name && (
                    <span className="hidden shrink-0 rounded bg-indigo-500/10 px-1.5 py-0.5 text-[10px] text-indigo-300 sm:inline">
                      {name}
                    </span>
                  )}
                  {t.thesis && (
                    <NotebookPen className="hidden h-3 w-3 shrink-0 text-slate-500 sm:block" />
                  )}

                  <span className="ml-auto shrink-0 font-mono text-[10px] text-obsidian-muted">
                    {dateFormatter.format(new Date(t.entry_date))}
                  </span>
                </button>

                {expanded === t.id && (
                  <div className="space-y-3 border-t border-obsidian-border px-4 py-3">
                    <label className="block">
                      <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
                        Strategy
                      </span>
                      <select
                        value={d.strategyId}
                        onChange={(e) =>
                          setDrafts((p) => ({
                            ...p,
                            [t.id]: { ...d, strategyId: e.target.value },
                          }))
                        }
                        className={`mt-1 ${fieldClass}`}
                      >
                        <option value="">— None —</option>
                        {(strategies ?? []).map((s) => (
                          <option key={s.id} value={s.id}>
                            {s.name}
                          </option>
                        ))}
                      </select>
                    </label>

                    <label className="block">
                      <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
                        Why this trade?
                      </span>
                      <textarea
                        value={d.thesis}
                        onChange={(e) =>
                          setDrafts((p) => ({
                            ...p,
                            [t.id]: { ...d, thesis: e.target.value },
                          }))
                        }
                        rows={3}
                        placeholder="Setup, trigger, and what would prove you wrong."
                        className={`mt-1 resize-y ${fieldClass}`}
                      />
                    </label>

                    <div className="flex items-center justify-between">
                      <span className="text-[10px] text-obsidian-muted">
                        {t.source_tag === 'Manual'
                          ? 'Hand-logged'
                          : 'Synced from IBKR — add the reasoning it cannot know'}
                      </span>
                      <button
                        type="button"
                        onClick={() => save(t)}
                        disabled={annotate.isPending}
                        className="rounded-lg border border-win-border bg-win-glow px-3 py-1.5 text-[11px] text-win disabled:opacity-50"
                      >
                        {annotate.isPending ? 'Saving…' : 'Save'}
                      </button>
                    </div>
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

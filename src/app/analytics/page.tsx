'use client';

import React, { useEffect, useState } from 'react';
import Link from 'next/link';
import {
  AlertCircle,
  ArrowLeft,
  BarChart3,
  Check,
  CheckCircle2,
  Loader2,
  Target,
  TrendingDown,
  X,
} from 'lucide-react';

import {
  useAdvancedMetrics,
  useReviewTrade,
  useTradeReviewQueue,
} from '@/hooks/useTradeInbox';
import type { AdvancedMetrics, TradeReview } from '@/types/api';

/** Common behavioural tags, offered as chips. Free text is also allowed. */
const MISTAKE_TAGS = [
  'FOMO',
  'Chased',
  'Early Liquidation',
  'Moved Stop',
  'Oversized',
  'No Plan',
  'Revenge Trade',
  'Hesitated',
] as const;

/** Renders a nullable metric without pretending null means zero. */
function metric(value: number | null | undefined, suffix = '', digits = 2): string {
  if (value === null || value === undefined) return '—';
  return `${value.toFixed(digits)}${suffix}`;
}

function KpiCard({
  label,
  value,
  hint,
  tone = 'neutral',
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: 'neutral' | 'win' | 'loss';
}) {
  const toneClass =
    tone === 'win' ? 'text-win' : tone === 'loss' ? 'text-loss' : 'text-white';
  return (
    <div className="p-4 rounded-xl border border-obsidian-border bg-obsidian-card">
      <span className="text-[11px] font-medium text-obsidian-muted uppercase tracking-wider">
        {label}
      </span>
      <div className={`mt-2 text-2xl font-bold font-mono ${toneClass}`}>{value}</div>
      {hint && <p className="mt-1 text-[10px] text-obsidian-muted">{hint}</p>}
    </div>
  );
}

function RDistribution({ metrics }: { metrics: AdvancedMetrics }) {
  const dist = metrics.r_distribution ?? {};
  const entries = Object.entries(dist);
  const max = Math.max(1, ...entries.map(([, n]) => n));

  return (
    <div className="p-5 rounded-xl border border-obsidian-border bg-obsidian-card">
      <h3 className="text-sm font-semibold text-slate-200 mb-4">R-Distribution</h3>
      <div className="space-y-2">
        {entries.map(([bucket, count]) => {
          const isLoss = bucket.startsWith('<') || bucket.startsWith('-');
          return (
            <div key={bucket} className="flex items-center gap-3">
              <span className="w-20 shrink-0 text-[11px] font-mono text-obsidian-muted">
                {bucket}
              </span>
              <div className="flex-1 h-5 rounded bg-obsidian-bg border border-obsidian-border overflow-hidden">
                <div
                  className={`h-full ${isLoss ? 'bg-loss/40' : 'bg-win/40'}`}
                  style={{ width: `${(count / max) * 100}%` }}
                />
              </div>
              <span className="w-8 text-right text-[11px] font-mono text-slate-300">
                {count}
              </span>
            </div>
          );
        })}
      </div>
      {metrics.unscored_trades > 0 && (
        <p className="mt-3 text-[10px] text-obsidian-muted">
          {metrics.unscored_trades} trade
          {metrics.unscored_trades === 1 ? '' : 's'} not scored — still open, or
          missing a usable stop loss.
        </p>
      )}
    </div>
  );
}

function MistakeBreakdown({ metrics }: { metrics: AdvancedMetrics }) {
  const rows = metrics.mistake_breakdown ?? [];

  return (
    <div className="p-5 rounded-xl border border-obsidian-border bg-obsidian-card">
      <h3 className="text-sm font-semibold text-slate-200 mb-4">
        Performance by Mistake
      </h3>
      {rows.length === 0 ? (
        <p className="text-xs text-obsidian-muted py-4 text-center">
          No tagged mistakes yet. Review trades to build this breakdown.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-obsidian-muted text-[10px] uppercase tracking-wider">
                <th className="text-left font-medium pb-2">Mistake</th>
                <th className="text-right font-medium pb-2">Trades</th>
                <th className="text-right font-medium pb-2">Total R</th>
                <th className="text-right font-medium pb-2">Avg R</th>
                <th className="text-right font-medium pb-2">Win %</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.mistake} className="border-t border-obsidian-border">
                  <td className="py-2 text-slate-200">{row.mistake}</td>
                  <td className="py-2 text-right font-mono text-slate-300">
                    {row.trade_count}
                  </td>
                  <td
                    className={`py-2 text-right font-mono font-semibold ${
                      row.total_r >= 0 ? 'text-win' : 'text-loss'
                    }`}
                  >
                    {row.total_r.toFixed(2)}R
                  </td>
                  <td
                    className={`py-2 text-right font-mono ${
                      row.avg_r >= 0 ? 'text-win' : 'text-loss'
                    }`}
                  >
                    {row.avg_r.toFixed(2)}R
                  </td>
                  <td className="py-2 text-right font-mono text-slate-300">
                    {row.win_rate_pct}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function ReviewDrawer({
  trade,
  onClose,
}: {
  trade: TradeReview | null;
  onClose: () => void;
}) {
  const [notes, setNotes] = useState('');
  const [tags, setTags] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const mutation = useReviewTrade();

  // Reload the draft whenever a different trade is opened.
  useEffect(() => {
    if (trade) {
      setNotes(trade.notes ?? '');
      setTags(trade.mistakes ?? []);
      setError(null);
    }
  }, [trade]);

  if (!trade) return null;

  const toggleTag = (tag: string) =>
    setTags((prev) =>
      prev.includes(tag) ? prev.filter((t) => t !== tag) : [...prev, tag]
    );

  const handleSave = () => {
    setError(null);
    mutation.mutate(
      { id: trade.id, payload: { notes, mistakes: tags } },
      { onSuccess: onClose, onError: (e) => setError(e.message) }
    );
  };

  const isSaving = mutation.isPending;

  return (
    <div className="fixed inset-0 z-[100] flex justify-end" role="dialog" aria-modal="true">
      <div
        className="absolute inset-0 bg-black/70 backdrop-blur-sm"
        onClick={() => !isSaving && onClose()}
      />
      <div className="relative h-full w-full max-w-md bg-obsidian-card border-l border-obsidian-border overflow-y-auto">
        <div className="flex items-center justify-between px-5 py-4 border-b border-obsidian-border sticky top-0 bg-obsidian-card">
          <div>
            <h2 className="text-sm font-semibold text-slate-100">
              Review {trade.ticker}
            </h2>
            <p className="text-[11px] text-obsidian-muted font-mono">
              {trade.direction} {trade.quantity} @ {trade.actual_entry}
              {trade.exit_price !== null && ` → ${trade.exit_price}`}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={isSaving}
            aria-label="Close"
            className="p-1 rounded-lg text-obsidian-muted hover:text-slate-200 disabled:opacity-50"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="px-5 py-5 space-y-5">
          <div>
            <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
              What happened?
            </span>
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              disabled={isSaving}
              rows={8}
              placeholder="What was the setup? What did you see? What would you do differently?"
              className="mt-1 w-full rounded-lg bg-obsidian-bg border border-obsidian-border px-3 py-2 text-xs text-slate-200 leading-relaxed placeholder:text-obsidian-muted focus:outline-none focus:border-slate-600 resize-y disabled:opacity-50"
            />
          </div>

          <div>
            <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
              Behavioural Tags
            </span>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {MISTAKE_TAGS.map((tag) => {
                const active = tags.includes(tag);
                return (
                  <button
                    key={tag}
                    type="button"
                    onClick={() => toggleTag(tag)}
                    disabled={isSaving}
                    aria-pressed={active}
                    className={`rounded-full border px-2.5 py-1 text-[11px] transition-colors disabled:opacity-50 ${
                      active
                        ? 'border-loss/50 bg-loss/15 text-loss'
                        : 'border-obsidian-border bg-obsidian-bg text-obsidian-muted hover:text-slate-200 hover:border-slate-600'
                    }`}
                  >
                    {tag}
                  </button>
                );
              })}
            </div>
            {tags.length > 0 && (
              <p className="mt-2 text-[10px] text-obsidian-muted">
                {tags.length} tag{tags.length === 1 ? '' : 's'} selected
              </p>
            )}
          </div>

          {error && (
            <div className="flex items-start text-xs text-loss">
              <AlertCircle className="h-3.5 w-3.5 mr-1.5 mt-px shrink-0" />
              {error}
            </div>
          )}

          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              disabled={isSaving}
              className="rounded-lg border border-obsidian-border bg-obsidian-bg px-3.5 py-2 text-xs text-obsidian-muted hover:text-slate-200 disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleSave}
              disabled={isSaving}
              className="inline-flex items-center gap-2 rounded-lg border border-win-border bg-win-glow px-4 py-2 text-xs font-medium text-win hover:bg-win/20 disabled:opacity-60"
            >
              {isSaving ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  Saving…
                </>
              ) : (
                <>
                  <Check className="h-3.5 w-3.5" />
                  Mark Reviewed
                </>
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function AnalyticsPage() {
  const metricsQuery = useAdvancedMetrics();
  const queueQuery = useTradeReviewQueue('pending');
  const [selected, setSelected] = useState<TradeReview | null>(null);

  const m = metricsQuery.data;
  const queue = queueQuery.data ?? [];

  return (
    <div className="min-h-screen bg-obsidian-bg text-slate-100 flex flex-col font-sans">
      <header className="border-b border-obsidian-border bg-obsidian-card/80 backdrop-blur-md sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <div className="h-9 w-9 rounded-xl bg-gradient-to-br from-win/20 to-emerald-900/40 border border-win/30 flex items-center justify-center">
              <BarChart3 className="h-5 w-5 text-win" />
            </div>
            <div>
              <span className="font-bold text-lg tracking-wider text-white">
                ANALYTICS &amp; REVIEW
              </span>
              <p className="text-xs text-obsidian-muted font-medium">
                R-multiples, slippage, and behavioural attribution
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

      <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-8">
        {/* KPI cards */}
        {metricsQuery.isPending ? (
          <div className="flex items-center justify-center py-12 text-obsidian-muted">
            <Loader2 className="h-5 w-5 animate-spin mr-2" />
            <span className="text-sm">Loading metrics…</span>
          </div>
        ) : metricsQuery.isError ? (
          <div className="flex items-center justify-center py-12 text-loss">
            <AlertCircle className="h-5 w-5 mr-2" />
            <span className="text-sm">
              {metricsQuery.error instanceof Error
                ? metricsQuery.error.message
                : 'Failed to load metrics.'}
            </span>
          </div>
        ) : m ? (
          <>
            <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-4">
              <KpiCard
                label="Total R"
                value={metric(m.total_r, 'R')}
                hint={`${m.scored_trades} scored trade${m.scored_trades === 1 ? '' : 's'}`}
                tone={m.total_r >= 0 ? 'win' : 'loss'}
              />
              <KpiCard
                label="Expectancy"
                value={metric(m.expectancy_r, 'R')}
                hint="Per unit risked"
                tone={
                  m.expectancy_r === null
                    ? 'neutral'
                    : m.expectancy_r >= 0
                      ? 'win'
                      : 'loss'
                }
              />
              <KpiCard
                label="Profit Factor"
                // null = no losing trades; rendering 0.00 would invert the meaning.
                value={m.profit_factor_r === null ? '∞' : metric(m.profit_factor_r)}
                hint="Gross win R / loss R"
              />
              <KpiCard
                label="Win Rate"
                value={metric(m.win_rate_pct, '%')}
                hint="Of scored trades"
              />
              <KpiCard
                label="Avg Slippage"
                value={metric(m.avg_slippage, '', 4)}
                hint={
                  m.avg_slippage === null
                    ? 'No planned entries yet'
                    : `${m.slippage_sample} planned · + is worse`
                }
                tone={
                  m.avg_slippage === null
                    ? 'neutral'
                    : m.avg_slippage > 0
                      ? 'loss'
                      : 'win'
                }
              />
            </section>

            <section className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              <RDistribution metrics={m} />
              <MistakeBreakdown metrics={m} />
            </section>
          </>
        ) : null}

        {/* Pending review queue */}
        <section className="p-5 rounded-xl border border-obsidian-border bg-obsidian-card">
          <div className="flex items-center space-x-2 mb-5">
            <Target className="h-4 w-4 text-obsidian-muted" />
            <h2 className="text-sm font-semibold tracking-wide text-slate-200">
              PENDING REVIEW QUEUE
            </h2>
            {!queueQuery.isPending && (
              <span className="text-[10px] font-mono text-obsidian-muted">
                {queue.length}
              </span>
            )}
          </div>

          {queueQuery.isPending ? (
            <div className="flex items-center justify-center py-10 text-obsidian-muted">
              <Loader2 className="h-4 w-4 animate-spin mr-2" />
              <span className="text-xs">Loading queue…</span>
            </div>
          ) : queueQuery.isError ? (
            <div className="flex items-center justify-center py-10 text-loss text-xs">
              <AlertCircle className="h-4 w-4 mr-1.5" />
              {queueQuery.error instanceof Error
                ? queueQuery.error.message
                : 'Failed to load queue.'}
            </div>
          ) : queue.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-10 text-center">
              <div className="h-12 w-12 rounded-full bg-win/10 border border-win/30 flex items-center justify-center mb-3">
                <CheckCircle2 className="h-6 w-6 text-win" />
              </div>
              <p className="text-sm font-medium text-slate-200">
                Every trade reviewed
              </p>
              <p className="text-xs text-obsidian-muted mt-1">
                Nothing waiting for a qualitative pass.
              </p>
            </div>
          ) : (
            <ul className="space-y-2">
              {queue.map((trade) => (
                <li key={trade.id}>
                  <button
                    type="button"
                    onClick={() => setSelected(trade)}
                    className="w-full flex items-center justify-between rounded-lg border border-obsidian-border bg-obsidian-bg/50 px-4 py-3 text-left hover:border-slate-600 transition-colors"
                  >
                    <div className="flex items-center gap-3">
                      <span className="font-semibold text-slate-100">
                        {trade.ticker}
                      </span>
                      <span
                        className={`text-[10px] font-mono px-1.5 py-0.5 rounded border ${
                          trade.direction === 'BUY'
                            ? 'border-win/30 bg-win/10 text-win'
                            : 'border-loss/30 bg-loss/10 text-loss'
                        }`}
                      >
                        {trade.direction}
                      </span>
                      <span className="text-[11px] font-mono text-obsidian-muted">
                        {trade.quantity} @ {trade.actual_entry}
                        {trade.stop_loss !== null && ` · stop ${trade.stop_loss}`}
                      </span>
                    </div>
                    <div className="flex items-center gap-2">
                      {trade.exit_price === null && (
                        <span className="text-[10px] text-obsidian-muted">open</span>
                      )}
                      <TrendingDown className="h-3.5 w-3.5 text-obsidian-muted" />
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      </main>

      <ReviewDrawer trade={selected} onClose={() => setSelected(null)} />
    </div>
  );
}

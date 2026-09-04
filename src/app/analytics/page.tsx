'use client';

import React, { useEffect, useState } from 'react';
import Link from 'next/link';
import {
  AlertCircle,
  Check,
  CheckCircle2,
  Loader2,
  Target,
  TrendingDown,
  X,
} from 'lucide-react';

import {
  useAdvancedMetrics,
  usePendingPositions,
  useReviewPosition,
} from '@/hooks/useTradeInbox';
import {
  DEFAULT_SELECTION,
  TimeframeToolbar,
} from '@/components/TimeframeToolbar';
import { formatDuration } from '@/lib/format';
import type {
  AdvancedMetrics,
  Position,
  TimeframeSelection,
} from '@/types/api';

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

// A journal can span years, so unlike TradeLedger.tsx's plan timestamps
// (always fresh to the trade being viewed) this needs the year spelled out --
// "Aug 15" alone would misdate anything traded before this year.
const lastTradedFormatter = new Intl.DateTimeFormat('en-US', {
  month: 'short',
  day: '2-digit',
  year: 'numeric',
  timeZone: 'America/New_York',
});

/**
 * Which playbook entries are actually earning their place.
 *
 * A diverging bar chart of total R per strategy: profitable setups extend
 * right, losing ones left, ranked so the chart reads top-to-bottom as a verdict.
 *
 * Total R rather than win rate or dollars, because it is the only measure that
 * compares setups fairly. Win rate flatters a strategy that scratches often and
 * loses big; dollars flatter whichever setup happened to be sized largest.
 * Total R answers the question being asked -- per unit of risk committed to
 * this setup, what came back.
 *
 * Each label links to the playbook entry, and the join is on strategy_id, so
 * renaming a strategy there carries its whole history with it.
 */
function StrategyBreakdownChart({ metrics }: { metrics: AdvancedMetrics }) {
  const rows = metrics.strategy_breakdown ?? [];
  if (rows.length === 0) {
    return (
      <div className="p-5 rounded-xl border border-obsidian-border bg-obsidian-card">
        <h3 className="text-sm font-semibold text-slate-200 mb-1">
          Which Strategies Are Working
        </h3>
        <p className="text-xs text-obsidian-muted py-6 text-center">
          No scored trades yet. Assign a strategy and record a stop to build
          this.
        </p>
      </div>
    );
  }

  // Symmetric scale so a +3R bar and a -3R bar are drawn the same length --
  // an asymmetric axis would make the losing side look smaller than it is.
  const span = Math.max(1, ...rows.map((r) => Math.abs(r.total_r)));
  const axis = Math.ceil(span);
  const half = (value: number) => (Math.abs(value) / axis) * 50;

  const ticks = [-axis, -axis / 2, 0, axis / 2, axis];

  return (
    <div className="p-5 rounded-xl border border-obsidian-border bg-obsidian-card">
      <h3 className="text-sm font-semibold text-slate-200 mb-1">
        Which Strategies Are Working
      </h3>
      <p className="text-[11px] text-obsidian-muted mb-5">
        Total R per playbook entry. Longer right is better; each label links to
        the playbook.
      </p>

      <div className="space-y-1.5">
        {rows.map((row) => {
          const positive = row.total_r >= 0;
          const width = half(row.total_r);
          const isUnassigned = row.strategy === 'Unassigned';

          // Neither fits in the bar itself: a single total R says nothing
          // about whether it came from one lucky trade or twenty steady
          // ones, and the bar has no axis for time at all. best_r/worst_r
          // are null together, exactly when there is nothing scored to
          // range over.
          const detail: string[] = [];
          if (row.best_r !== null && row.worst_r !== null) {
            detail.push(
              `best ${row.best_r >= 0 ? '+' : ''}${row.best_r.toFixed(2)}R`,
              `worst ${row.worst_r >= 0 ? '+' : ''}${row.worst_r.toFixed(2)}R`
            );
          }
          if (row.last_traded !== null) {
            detail.push(
              `last traded ${lastTradedFormatter.format(new Date(row.last_traded))}`
            );
          }

          return (
            <div key={row.strategy}>
              <div className="flex items-center gap-3 group">
                <div className="w-40 shrink-0 text-right">
                  {isUnassigned ? (
                    <span
                      className="text-[11px] text-obsidian-muted italic"
                      title="Trades with no strategy set — assign one in the journal"
                    >
                      {row.strategy} ({row.trade_count})
                    </span>
                  ) : (
                    <Link
                      href="/strategies"
                      className="text-[11px] text-slate-300 hover:text-white hover:underline"
                      title={`Open "${row.strategy}" in the strategy playbook`}
                    >
                      {row.strategy} ({row.trade_count})
                    </Link>
                  )}
                </div>

                {/* Plot area: 50% either side of a centre zero line. */}
                <div className="relative h-7 flex-1 rounded bg-obsidian-bg/60">
                  <div className="absolute inset-y-0 left-1/2 w-px bg-obsidian-border" />
                  <div
                    className={`absolute inset-y-1 rounded-sm transition-opacity group-hover:opacity-90 ${
                      positive ? 'bg-blue-500' : 'bg-loss'
                    }`}
                    style={
                      positive
                        ? { left: '50%', width: `${width}%` }
                        : { right: '50%', width: `${width}%` }
                    }
                    title={
                      `${row.strategy}: ${row.total_r >= 0 ? '+' : ''}${row.total_r.toFixed(2)}R ` +
                      `over ${row.scored} scored trade${row.scored === 1 ? '' : 's'}` +
                      (row.unscored ? ` (${row.unscored} unscored)` : '') +
                      (row.avg_r !== null ? ` · avg ${row.avg_r.toFixed(2)}R` : '') +
                      (row.win_rate_pct !== null ? ` · win ${row.win_rate_pct}%` : '')
                    }
                  />
                </div>

                <span
                  className={`w-16 shrink-0 text-right font-mono text-[11px] ${
                    positive ? 'text-blue-400' : 'text-loss'
                  }`}
                >
                  {row.total_r >= 0 ? '+' : ''}
                  {row.total_r.toFixed(2)}R
                </span>
              </div>

              {detail.length > 0 && (
                <div className="flex items-center gap-3">
                  <div className="w-40 shrink-0" />
                  <p className="mt-0.5 flex-1 text-[10px] text-obsidian-muted">
                    {detail.join(' · ')}
                  </p>
                  <div className="w-16 shrink-0" />
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Axis */}
      <div className="mt-2 flex items-center gap-3">
        <div className="w-40 shrink-0" />
        <div className="relative h-4 flex-1">
          {ticks.map((t) => (
            <span
              key={t}
              className="absolute -translate-x-1/2 font-mono text-[10px] text-obsidian-muted"
              style={{ left: `${50 + (t / axis) * 50}%` }}
            >
              {t > 0 ? '+' : ''}
              {t}R
            </span>
          ))}
        </div>
        <div className="w-16 shrink-0" />
      </div>

      {/* Coverage: a strategy whose trades mostly lack stops cannot be scored,
          and saying so beats letting it sit near zero as if it were neutral. */}
      {rows.some((r) => r.unscored > 0) && (
        <p className="mt-4 text-[10px] text-obsidian-muted">
          {rows.reduce((n, r) => n + r.unscored, 0)} trade
          {rows.reduce((n, r) => n + r.unscored, 0) === 1 ? '' : 's'} could not
          be scored in R (no stop recorded) and contribute nothing to the bars
          above.
        </p>
      )}
    </div>
  );
}

/**
 * What each of the trader's own rules is measurably worth.
 *
 * The point of a discipline checklist is not the ticking, it is finding out
 * which rules earn their place. Each rule is split into the trades that
 * honoured it and the trades that did not, and the edge column is the gap
 * between them — a rule with no measurable edge is a superstition.
 *
 * Win rate is used as the headline rather than R because it needs only a
 * realised P&L. Keying this off R would leave the whole panel empty until
 * every trade carried a stop, which is exactly the state most journals are in.
 */
function DisciplineBreakdown({ metrics }: { metrics: AdvancedMetrics }) {
  const rows = metrics.discipline_breakdown ?? [];

  const pct = (value: number | null) => (value === null ? '—' : `${value}%`);
  // avg_r and r_sample are two separate fields on the wire, checked
  // together rather than assumed to always agree -- the same defensive
  // shape ComplianceBuckets below already uses for the identical pairing.
  //
  // The sample size is appended rather than hidden behind the number: `r_sample`
  // exists specifically (per its own backend docstring) so one scored trade
  // does not "look like a verdict on twenty" -- trade_count in the column next
  // to it can be far larger, since not every trade carries a stop to score R
  // against. Shown always, not just when small, so a reader learns the two
  // counts can differ rather than discovering it only on the row where it bites.
  const r = (value: number | null, sample: number) =>
    value === null || sample === 0
      ? '—'
      : `${value >= 0 ? '+' : ''}${value.toFixed(2)}R (n=${sample})`;

  return (
    <div className="p-5 rounded-xl border border-obsidian-border bg-obsidian-card">
      <h3 className="text-sm font-semibold text-slate-200 mb-1">
        Does Following Your Rules Pay?
      </h3>
      <p className="text-[11px] text-obsidian-muted mb-4">
        Win rate when you followed each rule, against when you didn&rsquo;t.
      </p>
      {rows.length === 0 ? (
        <p className="text-xs text-obsidian-muted py-4 text-center">
          No discipline answers yet. Review a closed trade to start this
          breakdown.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-obsidian-muted text-[10px] uppercase tracking-wider">
                <th className="text-left font-medium pb-2">Rule</th>
                <th className="text-right font-medium pb-2">Followed</th>
                <th className="text-right font-medium pb-2">Win %</th>
                <th className="text-right font-medium pb-2">Broke</th>
                <th className="text-right font-medium pb-2">Win %</th>
                <th className="text-right font-medium pb-2">Edge</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.discipline} className="border-t border-obsidian-border">
                  <td className="py-2 text-slate-200">{row.discipline}</td>
                  <td className="py-2 text-right font-mono text-slate-300">
                    {row.followed.trade_count}
                  </td>
                  <td className="py-2 text-right font-mono text-slate-300">
                    {pct(row.followed.win_rate_pct)}
                    {/* r_sample can be smaller than trade_count -- not every
                        trade carries a stop to score R against. */}
                    <div className="text-[10px] text-obsidian-muted">
                      {r(row.followed.avg_r, row.followed.r_sample)}
                    </div>
                  </td>
                  <td className="py-2 text-right font-mono text-slate-300">
                    {row.not_followed.trade_count}
                  </td>
                  <td className="py-2 text-right font-mono text-slate-300">
                    {pct(row.not_followed.win_rate_pct)}
                    <div className="text-[10px] text-obsidian-muted">
                      {r(row.not_followed.avg_r, row.not_followed.r_sample)}
                    </div>
                  </td>
                  {/* Null means one side has no trades, so there is nothing to
                      compare against. Shown as a dash rather than 0, which
                      would read as "this rule makes no difference". */}
                  <td
                    className={`py-2 text-right font-mono font-semibold ${
                      row.edge_win_rate_pct === null
                        ? 'text-obsidian-muted'
                        : row.edge_win_rate_pct >= 0
                          ? 'text-win'
                          : 'text-loss'
                    }`}
                    title={
                      row.edge_win_rate_pct === null
                        ? 'No comparison available — every reviewed trade fell on one side of this rule'
                        : 'Percentage points of win rate gained by following this rule'
                    }
                  >
                    {row.edge_win_rate_pct === null
                      ? '—'
                      : `${row.edge_win_rate_pct >= 0 ? '+' : ''}${row.edge_win_rate_pct} pts`}
                    {/* The R-based edge can disagree with the win-rate edge
                        above it -- a rule can win more often and still cost
                        more per trade, or the reverse -- which is the whole
                        reason to show both rather than only one. */}
                    <div
                      className={`text-[10px] font-normal ${
                        row.edge_r === null
                          ? 'text-obsidian-muted'
                          : row.edge_r >= 0
                            ? 'text-win'
                            : 'text-loss'
                      }`}
                      title={
                        row.edge_r === null
                          ? 'No comparison available — every reviewed trade fell on one side of this rule'
                          : 'R gained per trade by following this rule, against not following it'
                      }
                    >
                      {row.edge_r === null
                        ? '—'
                        : `${row.edge_r >= 0 ? '+' : ''}${row.edge_r.toFixed(2)}R`}
                    </div>
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

/**
 * The coarser question `DisciplineBreakdown` above can't answer: not "is
 * rule X worth following", but "does following your rules AS A WHOLE track
 * the outcome at all" — the headline comparison a compliance checklist
 * exists to make ("100% compliant = 68% win rate | under 50% = 25%").
 *
 * Fixed ranges, always all four, even ones with no trades in them yet — the
 * bucket a trade lands in does not depend on how many rules exist today, so
 * emptiness here is itself information rather than something to hide.
 */
function ComplianceBuckets({ metrics }: { metrics: AdvancedMetrics }) {
  const rows = metrics.compliance_buckets ?? [];
  const scored = rows.reduce((sum, r) => sum + r.trade_count, 0);

  const pct = (value: number | null) => (value === null ? '—' : `${value}%`);

  return (
    <div className="p-5 rounded-xl border border-obsidian-border bg-obsidian-card">
      <h3 className="text-sm font-semibold text-slate-200 mb-1">
        Does Overall Compliance Pay?
      </h3>
      <p className="text-[11px] text-obsidian-muted mb-4">
        Win rate grouped by how much of your answered playbook you followed on
        each trade.
      </p>
      {scored === 0 ? (
        <p className="text-xs text-obsidian-muted py-4 text-center">
          No trades reviewed against a rule yet.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-obsidian-muted text-[10px] uppercase tracking-wider">
                <th className="text-left font-medium pb-2">Compliance</th>
                <th className="text-right font-medium pb-2">Trades</th>
                <th className="text-right font-medium pb-2">Win %</th>
                <th className="text-right font-medium pb-2">Avg R</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.compliance} className="border-t border-obsidian-border">
                  <td className="py-2 text-slate-200">{row.compliance}</td>
                  <td className="py-2 text-right font-mono text-slate-300">
                    {row.trade_count}
                  </td>
                  <td className="py-2 text-right font-mono text-slate-300">
                    {pct(row.win_rate_pct)}
                  </td>
                  {/* r_sample can be smaller than trade_count -- not every
                      trade carries a stop to score R against -- so a
                      near-empty sample is shown as a dash rather than a
                      confident-looking number, and the sample size is
                      appended whenever a number IS shown, matching
                      DisciplineBreakdown's identical pairing above: this
                      column's own "Trades" count can be far larger than
                      how many of them actually back the R figure. */}
                  <td className="py-2 text-right font-mono text-slate-300">
                    {row.avg_r === null || row.r_sample === 0
                      ? '—'
                      : `${row.avg_r >= 0 ? '+' : ''}${row.avg_r.toFixed(2)}R (n=${row.r_sample})`}
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
                    {/* A trade with no stop still counts as a real instance
                        of the tag, but cannot be scored -- disclosed here
                        rather than silently dropped from the count, the
                        same way Strategy Breakdown discloses its own
                        unscored trades. */}
                    {row.unscored > 0 && (
                      <span className="ml-1 text-[10px] text-obsidian-muted">
                        ({row.unscored} unscored)
                      </span>
                    )}
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
                      row.avg_r === null
                        ? 'text-obsidian-muted'
                        : row.avg_r >= 0
                          ? 'text-win'
                          : 'text-loss'
                    }`}
                  >
                    {row.avg_r === null ? '—' : `${row.avg_r.toFixed(2)}R`}
                  </td>
                  <td className="py-2 text-right font-mono text-slate-300">
                    {row.win_rate_pct === null ? '—' : `${row.win_rate_pct}%`}
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
  position,
  onClose,
}: {
  position: Position | null;
  onClose: () => void;
}) {
  const [notes, setNotes] = useState('');
  const [tags, setTags] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const mutation = useReviewPosition();

  // Reload the draft whenever a different position is opened.
  useEffect(() => {
    if (position) {
      setNotes(position.notes ?? '');
      setTags(position.mistakes ?? []);
      setError(null);
    }
  }, [position]);

  if (!position) return null;

  const toggleTag = (tag: string) =>
    setTags((prev) =>
      prev.includes(tag) ? prev.filter((t) => t !== tag) : [...prev, tag]
    );

  const handleSave = () => {
    setError(null);
    mutation.mutate(
      { id: position.id, payload: { notes, mistakes: tags, mark_reviewed: false } },
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
              Review {position.symbol}
            </h2>
            <p className="text-[11px] text-obsidian-muted font-mono">
              {position.quantity} @ {position.entry_price} → {position.exit_price}
              {' · '}
              <span className={position.realized_pnl >= 0 ? 'text-win' : 'text-loss'}>
                {position.realized_pnl >= 0 ? '+' : ''}
                {position.realized_pnl.toFixed(2)}
              </span>
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
              onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) =>
                setNotes(e.target.value)
              }
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
  // This page's OWN selection, deliberately not shared with the dashboard.
  // The two answer different questions and get read at different times --
  // narrowing the dashboard to YTD to check the year so far should not
  // silently re-scope an R-distribution being studied over the last month.
  // Local state is what keeps them independent; nothing here is persisted,
  // for the same reason the dashboard's is not.
  const [timeframe, setTimeframe] = useState<TimeframeSelection>(DEFAULT_SELECTION);

  const metricsQuery = useAdvancedMetrics(timeframe);
  // Same hook the dashboard Trade Inbox uses -- one queue, one lifecycle.
  // Deliberately NOT windowed: the queue is a work list, and one that hid an
  // older unreviewed trade because of a filter set for a statistic would be
  // worse than a long one.
  const queueQuery = usePendingPositions();
  const [selected, setSelected] = useState<Position | null>(null);

  const m = metricsQuery.data;
  const queue = queueQuery.data ?? [];

  return (
    <div className="min-h-screen bg-obsidian-bg text-slate-100 flex flex-col font-sans">
      <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-8">
        {/* Above the KPI cards for the same reason it is on the dashboard:
            this governs every figure below it, and a control that scopes the
            whole page should not sit inside one panel of it. The review queue
            at the bottom is the one thing it does not reach. */}
        <section>
          <TimeframeToolbar
            selection={timeframe}
            onSelect={setTimeframe}
            window={m?.window}
            isFetching={metricsQuery.isFetching}
          />
        </section>

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
            {/* Three across rather than one row of six, which left each card
                about a sixth of the width and reading as small and far apart.
                Six cards over two rows of three doubles the width of each.

                Tied to the number of cards: a seventh would leave a row of
                one. Adding one means revisiting this, which is what the test
                asserting six-cards-in-three-columns is for. */}
            <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
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
                // Suffixed because the Dashboard shows a DIFFERENT profit
                // factor, computed on dollar P&L. The two legitimately differ
                // -- 0.95 here against 0.89 there -- because R weights every
                // trade by the risk it took rather than by its size.
                label="Profit Factor (R)"
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
              <KpiCard
                label="Journal Lag"
                // No sign here to reduce to win/loss on -- lower is simply
                // better, which formatDuration's own scale already conveys
                // without a colour needing to say it twice.
                value={
                  m.avg_journal_lag_hours === null
                    ? '—'
                    : formatDuration(m.avg_journal_lag_hours)
                }
                hint={
                  m.journal_lag_sample === 0
                    ? 'Nothing journaled yet'
                    : `${m.journal_lag_sample} journaled · close to write-up`
                }
              />
            </section>

            <section className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              <RDistribution metrics={m} />
              <MistakeBreakdown metrics={m} />
            </section>

            <section>
              <StrategyBreakdownChart metrics={m} />
            </section>

            <section className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              <DisciplineBreakdown metrics={m} />
              <ComplianceBuckets metrics={m} />
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
              {queue.map((position) => (
                <li key={position.id}>
                  <button
                    type="button"
                    onClick={() => setSelected(position)}
                    className="w-full flex items-center justify-between rounded-lg border border-obsidian-border bg-obsidian-bg/50 px-4 py-3 text-left hover:border-slate-600 transition-colors"
                  >
                    <div className="flex items-center gap-3">
                      <span className="font-semibold text-slate-100">
                        {position.symbol}
                      </span>
                      <span className="text-[10px] font-mono px-1.5 py-0.5 rounded border border-obsidian-border bg-obsidian-bg text-slate-300">
                        {position.style}
                      </span>
                      <span className="text-[11px] font-mono text-obsidian-muted">
                        {position.quantity} @ {position.entry_price} →{' '}
                        {position.exit_price}
                      </span>
                    </div>
                    <div className="flex items-center gap-2">
                      <span
                        className={`text-xs font-mono font-semibold ${
                          position.realized_pnl >= 0 ? 'text-win' : 'text-loss'
                        }`}
                      >
                        {position.realized_pnl >= 0 ? '+' : ''}
                        {position.realized_pnl.toFixed(2)}
                      </span>
                      <TrendingDown className="h-3.5 w-3.5 text-obsidian-muted" />
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      </main>

      <ReviewDrawer position={selected} onClose={() => setSelected(null)} />
    </div>
  );
}

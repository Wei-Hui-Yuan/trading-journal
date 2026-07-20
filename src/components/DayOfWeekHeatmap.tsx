'use client';

import React, { useState } from 'react';
import { AlertCircle, Calendar, Loader2, Sparkles } from 'lucide-react';

import type { DayName, Heatmap, HeatmapCell, SessionName } from '@/types/api';

interface DayOfWeekHeatmapProps {
  /** Grid straight from GET /api/analytics/dashboard. */
  data?: Heatmap;
  isLoading?: boolean;
  error?: Error | null;
}

/** Short labels for the row headers; full names key into the API payload. */
const DAY_LABELS: Record<DayName, string> = {
  Monday: 'Monday',
  Tuesday: 'Tuesday',
  Wednesday: 'Wednesday',
  Thursday: 'Thursday',
  Friday: 'Friday',
};

const money = (value: number) =>
  `${value >= 0 ? '+' : '-'}$${Math.abs(value).toLocaleString('en-US', {
    maximumFractionDigits: 2,
  })}`;

/**
 * A cell with trades but no losses at all.
 *
 * `profit_factor === null` is the backend's signal for "gross losses were
 * zero". Paired with positive P&L that is a flawless session, so it gets its
 * own treatment rather than blending into the ordinary green scale.
 */
function isPerfectSession(cell: HeatmapCell): boolean {
  return (
    cell.trade_count > 0 && cell.profit_factor === null && cell.net_pnl > 0
  );
}

function cellClasses(cell: HeatmapCell): string {
  if (cell.trade_count === 0) {
    return 'bg-obsidian-bg/60 text-obsidian-muted border-obsidian-border/50 hover:border-slate-600';
  }
  if (isPerfectSession(cell)) {
    // Gold: flawless execution, visually distinct from ordinary profit.
    return 'bg-amber-400/15 text-amber-300 border-amber-400/60 shadow-[0_0_16px_-3px_rgba(251,191,36,0.45)] hover:border-amber-300 font-semibold';
  }
  if (cell.net_pnl > 0) {
    return 'bg-win/20 text-win border-win/40 shadow-win-glow hover:border-win font-medium';
  }
  if (cell.net_pnl < 0) {
    return 'bg-loss/20 text-loss border-loss/40 shadow-loss-glow hover:border-loss font-semibold';
  }
  // Traded but net flat.
  return 'bg-slate-800/40 text-slate-300 border-slate-700 hover:border-slate-500';
}

const shell = (children: React.ReactNode, legend?: React.ReactNode) => (
  <div className="p-5 rounded-xl border border-obsidian-border bg-obsidian-card">
    <div className="flex flex-col sm:flex-row sm:items-center justify-between pb-4 border-b border-obsidian-border gap-2">
      <div className="flex items-center space-x-2">
        <Calendar className="h-5 w-5 text-win" />
        <h2 className="text-base font-semibold text-white">
          Day-of-Week Performance Heatmap
        </h2>
      </div>
      {legend}
    </div>
    {children}
  </div>
);

export const DayOfWeekHeatmap: React.FC<DayOfWeekHeatmapProps> = ({
  data,
  isLoading = false,
  error = null,
}) => {
  const [hovered, setHovered] = useState<{
    day: DayName;
    session: SessionName;
  } | null>(null);

  if (isLoading) {
    return shell(
      <div className="flex items-center justify-center py-16 text-obsidian-muted">
        <Loader2 className="h-5 w-5 animate-spin mr-2" />
        <span className="text-sm">Loading performance grid…</span>
      </div>
    );
  }

  if (error) {
    return shell(
      <div className="flex items-center justify-center py-16 text-loss">
        <AlertCircle className="h-5 w-5 mr-2" />
        <span className="text-sm">{error.message}</span>
      </div>
    );
  }

  if (!data) {
    return shell(
      <div className="flex items-center justify-center py-16 text-obsidian-muted">
        <span className="text-sm">No performance data yet.</span>
      </div>
    );
  }

  // Row/column order comes from the API so the grid always matches the
  // backend's bucketing, even if sessions are added or renamed later.
  const { days, sessions, cells, day_totals } = data;

  const legend = (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs">
      <div className="flex items-center space-x-1.5">
        <span className="h-3 w-3 rounded bg-amber-400/15 border border-amber-400/60 inline-block" />
        <span className="text-obsidian-muted">Perfect</span>
      </div>
      <div className="flex items-center space-x-1.5">
        <span className="h-3 w-3 rounded bg-win/20 border border-win/40 inline-block" />
        <span className="text-obsidian-muted">Profit</span>
      </div>
      <div className="flex items-center space-x-1.5">
        <span className="h-3 w-3 rounded bg-loss/20 border border-loss/40 inline-block" />
        <span className="text-obsidian-muted">Loss</span>
      </div>
      <div className="flex items-center space-x-1.5">
        <span className="h-3 w-3 rounded bg-obsidian-bg/60 border border-obsidian-border/50 inline-block" />
        <span className="text-obsidian-muted">No trades</span>
      </div>
    </div>
  );

  return shell(
    <>
      <div className="mt-4 overflow-x-auto">
        <div className="min-w-[640px]">
          {/* Session column headers */}
          <div
            className="grid gap-2 mb-2 text-center text-xs font-mono text-obsidian-muted uppercase tracking-wider"
            style={{
              gridTemplateColumns: `7rem repeat(${sessions.length}, minmax(0, 1fr))`,
            }}
          >
            <div />
            {sessions.map((session) => (
              <div
                key={session}
                className="py-1 bg-obsidian-bg/40 rounded border border-obsidian-border/30"
              >
                {session}
              </div>
            ))}
          </div>

          {/* One row per weekday */}
          {days.map((day) => {
            const dayTotal = day_totals[day]?.net_pnl ?? 0;

            return (
              <div
                key={day}
                className="grid gap-2 mb-2 items-center"
                style={{
                  gridTemplateColumns: `7rem repeat(${sessions.length}, minmax(0, 1fr))`,
                }}
              >
                <div className="pr-2 flex flex-col justify-center">
                  <span className="text-xs font-bold text-slate-200">
                    {DAY_LABELS[day] ?? day}
                  </span>
                  <span
                    className={`text-[10px] font-mono ${
                      dayTotal >= 0 ? 'text-win' : 'text-loss'
                    }`}
                  >
                    {money(dayTotal)}
                  </span>
                </div>

                {sessions.map((session) => {
                  const cell = cells[day]?.[session] ?? {
                    trade_count: 0,
                    net_pnl: 0,
                    win_rate_pct: 0,
                    profit_factor: 0,
                  };
                  const isHovered =
                    hovered?.day === day && hovered?.session === session;
                  const perfect = isPerfectSession(cell);

                  return (
                    <div
                      key={session}
                      onMouseEnter={() => setHovered({ day, session })}
                      onMouseLeave={() => setHovered(null)}
                      className={`relative p-3 rounded-lg border text-center transition-all duration-150 cursor-pointer ${cellClasses(
                        cell
                      )}`}
                    >
                      {perfect && (
                        <Sparkles className="absolute top-1 right-1 h-3 w-3 text-amber-300" />
                      )}

                      <div className="text-sm font-mono">
                        {cell.trade_count > 0 ? (
                          money(cell.net_pnl)
                        ) : (
                          <span className="opacity-40">-</span>
                        )}
                      </div>
                      <div className="text-[10px] font-mono opacity-75 mt-0.5">
                        {cell.trade_count > 0
                          ? `${cell.trade_count} trade${
                              cell.trade_count > 1 ? 's' : ''
                            }`
                          : 'No trades'}
                      </div>

                      {/* Tooltip */}
                      {isHovered && cell.trade_count > 0 && (
                        <div className="absolute z-20 bottom-full left-1/2 -translate-x-1/2 mb-2 px-3 py-2 bg-obsidian-card border border-slate-700 rounded-lg shadow-xl text-xs whitespace-nowrap text-left text-slate-200">
                          <div className="font-semibold text-white mb-1">
                            {day} &bull; {session}
                          </div>
                          <div>
                            P&L:{' '}
                            <span
                              className={
                                cell.net_pnl >= 0
                                  ? 'text-win font-bold'
                                  : 'text-loss font-bold'
                              }
                            >
                              {money(cell.net_pnl)}
                            </span>
                          </div>
                          <div>
                            Trades:{' '}
                            <span className="font-mono text-white">
                              {cell.trade_count}
                            </span>
                          </div>
                          <div>
                            Win rate:{' '}
                            <span className="font-mono text-white">
                              {cell.win_rate_pct}%
                            </span>
                          </div>
                          <div>
                            Profit factor:{' '}
                            <span className="font-mono text-white">
                              {cell.profit_factor === null
                                ? '∞'
                                : cell.profit_factor.toFixed(2)}
                            </span>
                          </div>
                          {perfect && (
                            <div className="mt-1 pt-1 border-t border-slate-700 text-amber-300 font-medium">
                              Perfect session — no losing trades
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            );
          })}
        </div>
      </div>

      {data.excluded_weekend_trades > 0 && (
        <p className="mt-3 text-[11px] text-obsidian-muted">
          {data.excluded_weekend_trades} weekend position
          {data.excluded_weekend_trades === 1 ? '' : 's'} excluded from the grid.
        </p>
      )}
      <p className="mt-1 text-[11px] text-obsidian-muted">
        Sessions bucketed in {data.timezone}.
      </p>
    </>,
    legend
  );
};

export default DayOfWeekHeatmap;

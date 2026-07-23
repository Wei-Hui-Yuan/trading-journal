'use client';

import React, { useMemo, useState } from 'react';
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { AlertCircle, Loader2, TrendingUp } from 'lucide-react';

import type { EquityCurve, EquityCurvePoint } from '@/types/api';

interface EquityCurveChartProps {
  /** Curve straight from GET /api/analytics/dashboard. */
  data?: EquityCurve;
  isLoading?: boolean;
  error?: Error | null;
}

const money = (value: number) =>
  `${value >= 0 ? '+' : '-'}$${Math.abs(value).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;

/** Axis ticks want to be short; $1.2k beats $1,234.56 at 10px. */
const compactMoney = (value: number) => {
  const abs = Math.abs(value);
  const sign = value < 0 ? '-' : '';
  if (abs >= 1000) return `${sign}$${(abs / 1000).toFixed(1)}k`;
  return `${sign}$${abs.toFixed(0)}`;
};

const dayLabel = new Intl.DateTimeFormat('en-US', {
  month: 'short',
  day: 'numeric',
  timeZone: 'UTC',
});

const fullDayLabel = new Intl.DateTimeFormat('en-US', {
  weekday: 'short',
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  timeZone: 'UTC',
});

/**
 * Parsed as UTC on purpose.
 *
 * The backend already bucketed these into market-time calendar days, so the
 * string is a date, not an instant. `new Date('2026-01-05')` is midnight UTC,
 * and formatting that in a timezone behind UTC would render it as Jan 4 —
 * shifting the entire curve back a day for anyone west of London.
 */
const asUTCDate = (iso: string) => new Date(`${iso}T00:00:00Z`);

const shell = (children: React.ReactNode, right?: React.ReactNode) => (
  <div className="rounded-xl border border-obsidian-border bg-obsidian-card p-5">
    <div className="mb-5 flex flex-wrap items-center justify-between gap-2">
      <div className="flex items-center space-x-2">
        <TrendingUp className="h-4 w-4 text-obsidian-muted" />
        <h2 className="text-sm font-semibold tracking-wide text-slate-200">
          Equity Curve
        </h2>
      </div>
      {right}
    </div>
    {children}
  </div>
);

const Stat: React.FC<{
  label: string;
  value: string;
  tone?: 'win' | 'loss' | 'neutral';
  hint?: string;
}> = ({ label, value, tone = 'neutral', hint }) => (
  <div title={hint}>
    <p className="text-[10px] uppercase tracking-wider text-obsidian-muted">
      {label}
    </p>
    <p
      className={`font-mono text-sm ${
        tone === 'win' ? 'text-win' : tone === 'loss' ? 'text-loss' : 'text-slate-200'
      }`}
    >
      {value}
    </p>
  </div>
);

const CurveTooltip: React.FC<{
  active?: boolean;
  payload?: Array<{ payload: EquityCurvePoint }>;
}> = ({ active, payload }) => {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;

  return (
    <div className="rounded-lg border border-obsidian-border bg-obsidian-card px-3 py-2 shadow-2xl">
      <p className="text-[11px] font-medium text-slate-200">
        {fullDayLabel.format(asUTCDate(p.date))}
      </p>
      <div className="mt-1.5 space-y-0.5 font-mono text-[11px]">
        <div className="flex justify-between gap-4">
          <span className="text-obsidian-muted">Cumulative</span>
          <span className={p.cumulative_pnl >= 0 ? 'text-win' : 'text-loss'}>
            {money(p.cumulative_pnl)}
          </span>
        </div>
        {/* Only on days something closed. A "$0.00 today" row on 217 quiet
            days is noise that buries the days that moved. */}
        {p.trades > 0 && (
          <div className="flex justify-between gap-4">
            <span className="text-obsidian-muted">
              This day ({p.trades} trade{p.trades === 1 ? '' : 's'})
            </span>
            <span className={p.realized_pnl >= 0 ? 'text-win' : 'text-loss'}>
              {money(p.realized_pnl)}
            </span>
          </div>
        )}
        {p.drawdown < 0 && (
          <div className="flex justify-between gap-4">
            <span className="text-obsidian-muted">Below peak</span>
            <span className="text-loss">{money(p.drawdown)}</span>
          </div>
        )}
      </div>
    </div>
  );
};

/**
 * Cumulative realised P&L over time, and the drawdown it went through.
 *
 * Labelled "realised P&L", never "account equity": true equity needs a
 * starting balance plus every deposit and withdrawal, none of which the broker
 * feed carries. Open positions are not in here either — this is money that has
 * actually been booked.
 *
 * The underwater strip below the curve is not decoration. Depth of drawdown
 * and speed of recovery are the two things a cumulative line renders badly on
 * its own, because a dip near the top of the range and a dip near the bottom
 * look identical once the axis rescales.
 */
export const EquityCurveChart: React.FC<EquityCurveChartProps> = ({
  data,
  isLoading,
  error,
}) => {
  const [showUnderwater, setShowUnderwater] = useState(true);

  const points = useMemo(() => data?.points ?? [], [data]);

  // Padded so the line never runs along the frame, where it reads as clipped.
  const domain = useMemo((): [number, number] => {
    if (points.length === 0) return [0, 1];
    const values = points.map((p) => p.cumulative_pnl);
    const lo = Math.min(0, ...values);
    const hi = Math.max(0, ...values);
    const pad = Math.max((hi - lo) * 0.1, 1);
    return [lo - pad, hi + pad];
  }, [points]);

  if (isLoading) {
    return shell(
      <div className="flex items-center justify-center py-16 text-obsidian-muted">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        <span className="text-xs">Loading equity curve…</span>
      </div>
    );
  }

  if (error) {
    return shell(
      <div className="flex items-start py-8 text-xs text-loss">
        <AlertCircle className="mr-1.5 h-4 w-4 shrink-0" />
        <span>{error.message}</span>
      </div>
    );
  }

  if (!data || points.length === 0) {
    return shell(
      <div className="py-12 text-center">
        <p className="text-sm text-slate-300">No closed trades yet.</p>
        <p className="mt-1 text-[11px] text-obsidian-muted">
          The curve plots realised P&amp;L, so it starts once a round trip closes.
        </p>
      </div>
    );
  }

  const s = data.summary;
  const ends = points[points.length - 1].cumulative_pnl;

  return shell(
    <>
      <div className="mb-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat
          label="Net Realised"
          value={money(s.net_pnl)}
          tone={s.net_pnl >= 0 ? 'win' : 'loss'}
          hint="Sum of every closed round trip. Open positions are not included."
        />
        <Stat
          label="Peak"
          value={money(s.peak_pnl)}
          hint="Highest the cumulative curve ever reached."
        />
        <Stat
          label="Max Drawdown"
          value={money(s.max_drawdown)}
          tone={s.max_drawdown < 0 ? 'loss' : 'neutral'}
          hint="Deepest fall from a high-water mark to the low that followed it."
        />
        <Stat
          label="Below Peak Now"
          value={s.current_drawdown < 0 ? money(s.current_drawdown) : 'At peak'}
          tone={s.current_drawdown < 0 ? 'loss' : 'win'}
          hint="How far the curve currently sits under its high-water mark."
        />
      </div>

      <div className="h-64 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart
            data={points}
            margin={{ top: 4, right: 8, left: 0, bottom: 0 }}
          >
            <defs>
              {/* Split at zero so profit and loss are not the same colour
                  above and below the line. */}
              <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="rgb(52,211,153)" stopOpacity={0.35} />
                <stop offset="100%" stopColor="rgb(52,211,153)" stopOpacity={0} />
              </linearGradient>
            </defs>

            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
            <XAxis
              dataKey="date"
              tickFormatter={(v: string) => dayLabel.format(asUTCDate(v))}
              stroke="#475569"
              tick={{ fontSize: 10, fill: '#64748b' }}
              minTickGap={48}
              tickLine={false}
            />
            <YAxis
              domain={domain}
              tickFormatter={compactMoney}
              stroke="#475569"
              tick={{ fontSize: 10, fill: '#64748b' }}
              tickLine={false}
              width={52}
            />
            <Tooltip content={<CurveTooltip />} />

            {/* Break-even. The single most important line on the chart. */}
            <ReferenceLine y={0} stroke="#64748b" strokeDasharray="4 4" />
            {/* The high-water mark the drawdown is measured from. */}
            {s.peak_pnl > 0 && (
              <ReferenceLine
                y={s.peak_pnl}
                stroke="rgb(52,211,153)"
                strokeOpacity={0.35}
                strokeDasharray="2 4"
              />
            )}

            <Area
              type="monotone"
              dataKey="cumulative_pnl"
              stroke={ends >= 0 ? 'rgb(52,211,153)' : 'rgb(248,113,113)'}
              strokeWidth={1.75}
              fill="url(#equityFill)"
              fillOpacity={ends >= 0 ? 1 : 0.12}
              // 300 points with a dot each is a solid bar, not a line.
              dot={false}
              activeDot={{ r: 3 }}
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      {showUnderwater && (
        <div className="mt-3">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[10px] uppercase tracking-wider text-obsidian-muted">
              Underwater — distance below the high-water mark
            </span>
            <button
              type="button"
              onClick={() => setShowUnderwater(false)}
              className="text-[10px] text-obsidian-muted transition-colors hover:text-slate-300"
            >
              Hide
            </button>
          </div>
          <div className="h-20 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart
                data={points}
                margin={{ top: 2, right: 8, left: 0, bottom: 0 }}
              >
                <defs>
                  <linearGradient id="underwaterFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="rgb(248,113,113)" stopOpacity={0} />
                    <stop
                      offset="100%"
                      stopColor="rgb(248,113,113)"
                      stopOpacity={0.4}
                    />
                  </linearGradient>
                </defs>
                <XAxis dataKey="date" hide />
                <YAxis
                  domain={[Math.min(s.max_drawdown * 1.1, -1), 0]}
                  tickFormatter={compactMoney}
                  stroke="#475569"
                  tick={{ fontSize: 9, fill: '#64748b' }}
                  tickLine={false}
                  width={52}
                  // Two ticks: the worst it got, and level. Deduped because a
                  // curve that has never been below its peak would otherwise
                  // render 0 twice, stacked on itself.
                  ticks={s.max_drawdown < 0 ? [s.max_drawdown, 0] : [0]}
                />
                <Tooltip content={<CurveTooltip />} />
                <ReferenceLine y={0} stroke="#64748b" strokeDasharray="4 4" />
                <Area
                  type="monotone"
                  dataKey="drawdown"
                  stroke="rgb(248,113,113)"
                  strokeWidth={1.25}
                  fill="url(#underwaterFill)"
                  dot={false}
                  activeDot={{ r: 3 }}
                  isAnimationActive={false}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {!showUnderwater && (
        <button
          type="button"
          onClick={() => setShowUnderwater(true)}
          className="mt-2 text-[10px] text-obsidian-muted transition-colors hover:text-slate-300"
        >
          Show underwater chart
        </button>
      )}

      <p className="mt-3 text-[10px] leading-relaxed text-obsidian-muted">
        Cumulative <span className="text-slate-400">realised</span> P&amp;L, dated
        by when each round trip closed — {s.closed_trades} trades over{' '}
        {s.trading_days} trading days within {s.calendar_days} calendar days.
        Open positions are not included, and this is not account equity: the
        broker feed carries no deposits or withdrawals to anchor one to.
      </p>
    </>
  );
};

export default EquityCurveChart;

'use client';

import React, { useState } from 'react';
import dynamic from 'next/dynamic';
import { AlertCircle, Loader2 } from 'lucide-react';

import { Header } from '@/components/Header';
import { KPIStatStrip } from '@/components/KPIStatStrip';
import { DayOfWeekHeatmap } from '@/components/DayOfWeekHeatmap';
import { TradeInboxQueue } from '@/components/TradeInboxQueue';
import { OpenPlansDock } from '@/components/OpenPlansDock';
import {
  DEFAULT_SELECTION,
  TimeframeToolbar,
} from '@/components/TimeframeToolbar';

/**
 * Loaded on demand: Recharts pulls in d3 and costs ~100 kB, which is a third
 * of the dashboard's bundle for one chart that sits below the fold. Splitting
 * it out keeps the KPI strip — the part you actually open this page for —
 * painting on the original payload.
 *
 * `ssr: false` because Recharts measures the DOM to size itself; there is no
 * width to measure on the server, and prerendering it only produces markup
 * that is thrown away on hydration.
 */
const EquityCurveChart = dynamic(
  () => import('@/components/EquityCurveChart').then((m) => m.EquityCurveChart),
  {
    ssr: false,
    loading: () => (
      <div className="flex h-[26rem] items-center justify-center rounded-xl border border-obsidian-border bg-obsidian-card text-obsidian-muted">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        <span className="text-xs">Loading equity curve…</span>
      </div>
    ),
  }
);
import type { KPIStats, TimeframeSelection } from '@/types/api';
import {
  usePendingPositions,
  useDashboardStats,
  useAdvancedMetrics,
} from '@/hooks/useTradeInbox';

/**
 * The API host this build actually talks to.
 *
 * Mirrors the fallback in lib/api.ts. Inlined at build time by Next, so it
 * reflects the environment the bundle was built for — which is the point:
 * a missing NEXT_PUBLIC_API_URL is otherwise invisible until requests fail.
 */
const API_HOST = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

const EMPTY_STATS: KPIStats = {
  netPnl: 0,
  grossPnl: 0,
  openRunPnl: 0,
  ibCommission: 0,
  unverifiedLegs: 0,
  totalCommission: 0,
  winRate: 0,
  totalTrades: 0,
  profitFactor: 0,
  avgRoi: 0,
  avgR: null,
  avgRSample: 0,
  pendingCount: 0,
};

export default function Home() {
  // The window every figure on this page is computed over. Defaults to 1Y, so
  // the first paint is bounded rather than plotting the whole ledger.
  //
  // Held here rather than inside the toolbar because it governs all three
  // panels below — the strip, the heatmap and the curve read one payload, and
  // that is what stops them describing different spans on one screen.
  //
  // Deliberately not persisted. "What has the last year looked like" is the
  // question the dashboard exists to open on, and a window remembered from a
  // one-off investigation last week is a figure you would read as current.
  const [timeframe, setTimeframe] = useState<TimeframeSelection>(DEFAULT_SELECTION);

  // Both queries are served from the React Query cache, so mounting the inbox
  // and the stat strip does not double-fetch.
  const dashboardQuery = useDashboardStats(timeframe);
  const pendingQuery = usePendingPositions();
  // The SAME query the Analytics tab runs for this window -- avg_r has no
  // home in core_stats, which has no notion of R at all, so this is a second
  // request rather than a field threaded through the dashboard endpoint. No
  // loading/error UI of its own: like pendingQuery below, it degrades to the
  // strip's own null handling while pending, which is the same tradeoff
  // TradeLedger.tsx's secondary queries already make.
  const metricsQuery = useAdvancedMetrics(timeframe);

  const pendingCount = pendingQuery.data?.length ?? 0;
  const avgR = metricsQuery.data?.avg_r ?? null;
  const avgRSample = metricsQuery.data?.scored_trades ?? 0;

  // Map the API's snake_case core stats onto the strip's view model.
  const core = dashboardQuery.data?.core_stats;
  const kpiStats: KPIStats = core
    ? {
        netPnl: core.net_pnl,
        grossPnl: core.gross_pnl ?? core.net_pnl,
        openRunPnl: core.open_run_pnl ?? 0,
        ibCommission: core.ib_commission ?? 0,
        unverifiedLegs: core.unverified_legs ?? 0,
        totalCommission: core.total_commission ?? 0,
        winRate: core.win_rate_pct,
        totalTrades: core.total_trades,
        // Passed through as null rather than coerced to 0: the strip renders
        // an unbounded ratio as infinity.
        profitFactor: core.profit_factor,
        avgRoi: core.avg_roi_pct,
        avgR,
        avgRSample,
        pendingCount,
      }
    : { ...EMPTY_STATS, avgR, avgRSample, pendingCount };

  return (
    <div className="min-h-screen bg-obsidian-bg text-slate-100 flex flex-col font-sans">
      {/* Navigation Topbar */}
      <Header pendingCount={pendingCount} />

      {/* Main Dashboard Container */}
      <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-8">

        {/* Above the stat strip on purpose: this filters the strip, the
            heatmap and the curve alike, and a control that governs the whole
            page should not sit inside one panel of it. */}
        <section>
          <TimeframeToolbar
            selection={timeframe}
            onSelect={setTimeframe}
            window={dashboardQuery.data?.window}
            isFetching={dashboardQuery.isFetching}
          />
        </section>

        {/* Top KPI Stat Strip */}
        <section>
          {/* A failed request must not render as zeros. The strip has no way
              to express "unknown", so a dead API produced a confident
              "0 trades, $0.00" -- indistinguishable from an empty account,
              and the reason a broken backend looked like no trading history. */}
          {dashboardQuery.isError ? (
            <div className="flex items-center gap-2 rounded-xl border border-loss/30 bg-loss-glow px-4 py-3 text-sm text-loss">
              <AlertCircle className="h-4 w-4 shrink-0" />
              <span>
                Could not load statistics —{' '}
                {(dashboardQuery.error as Error)?.message ?? 'request failed'}.
                Figures below are not zero, they are unknown.
              </span>
            </div>
          ) : (
            <KPIStatStrip stats={kpiStats} />
          )}
        </section>

        {/* Day-of-Week Heatmap Layout Grid */}
        <section>
          <DayOfWeekHeatmap
            data={dashboardQuery.data?.heatmap}
            isLoading={dashboardQuery.isPending}
            error={dashboardQuery.error as Error | null}
          />
        </section>

        {/* Below the heatmap because the two answer different questions: the
            heatmap is "when do I trade well", this is "how has it gone". */}
        <section>
          <EquityCurveChart
            data={dashboardQuery.data?.equity_curve}
            isLoading={dashboardQuery.isPending}
            error={dashboardQuery.error as Error | null}
          />
        </section>

        {/* Plans you have committed to but not yet entered. Sits above the
            inbox because it is the forward-looking half: the inbox is trades
            that already happened and need reviewing. Renders nothing at all
            when no plans are open. */}
        <OpenPlansDock />

        {/* Trade Inbox Queue */}
        <section>
          <TradeInboxQueue />
        </section>

      </main>

      {/* Footer.
          The backend host is read from the environment rather than hardcoded.
          The literal "http://localhost:8000" was printed even in production,
          where the app was talking to Northflank — it reads as a diagnostic
          but was pure decoration, and sent at least one debugging session
          chasing a misconfiguration that did not exist. */}
      <footer className="border-t border-obsidian-border py-4 text-center text-xs text-obsidian-muted">
        Trading Journal Platform &bull; FastAPI Backend at {API_HOST} &bull; Obsidian Engine
      </footer>
    </div>
  );
}

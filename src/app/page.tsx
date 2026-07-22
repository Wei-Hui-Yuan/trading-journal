'use client';

import React from 'react';
import { AlertCircle } from 'lucide-react';

import { Header } from '@/components/Header';
import { KPIStatStrip } from '@/components/KPIStatStrip';
import { DayOfWeekHeatmap } from '@/components/DayOfWeekHeatmap';
import { TradeInboxQueue } from '@/components/TradeInboxQueue';
import { OpenPlansDock } from '@/components/OpenPlansDock';
import type { KPIStats } from '@/types/api';
import {
  usePendingPositions,
  useDashboardStats,
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
  winRate: 0,
  totalTrades: 0,
  profitFactor: 0,
  avgRoi: 0,
  pendingCount: 0,
};

export default function Home() {
  // Both queries are served from the React Query cache, so mounting the inbox
  // and the stat strip does not double-fetch.
  const dashboardQuery = useDashboardStats();
  const pendingQuery = usePendingPositions();

  const pendingCount = pendingQuery.data?.length ?? 0;

  // Map the API's snake_case core stats onto the strip's view model.
  const core = dashboardQuery.data?.core_stats;
  const kpiStats: KPIStats = core
    ? {
        netPnl: core.net_pnl,
        winRate: core.win_rate_pct,
        totalTrades: core.total_trades,
        // Passed through as null rather than coerced to 0: the strip renders
        // an unbounded ratio as infinity.
        profitFactor: core.profit_factor,
        avgRoi: core.avg_roi_pct,
        pendingCount,
      }
    : { ...EMPTY_STATS, pendingCount };

  return (
    <div className="min-h-screen bg-obsidian-bg text-slate-100 flex flex-col font-sans">
      {/* Navigation Topbar */}
      <Header pendingCount={pendingCount} />

      {/* Main Dashboard Container */}
      <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-8">

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

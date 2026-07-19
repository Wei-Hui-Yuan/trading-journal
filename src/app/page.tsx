'use client';

import React, { useState, useEffect } from 'react';
import { Header } from '@/components/Header';
import { KPIStatStrip } from '@/components/KPIStatStrip';
import { DayOfWeekHeatmap } from '@/components/DayOfWeekHeatmap';
import { TradeInbox } from '@/components/TradeInbox';
import { mockTrades, mockKPIStats, mockHeatmapData } from '@/data/mockTrades';
import { Trade, KPIStats } from '@/types/trade';
import { fetchTradesFromAPI, completeTradeAPI, computeKPIStats } from '@/lib/api';

export default function Home() {
  const [trades, setTrades] = useState<Trade[]>(mockTrades);
  const [kpiStats, setKpiStats] = useState<KPIStats>(mockKPIStats);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [isApiConnected, setIsApiConnected] = useState<boolean>(false);
  const [fetchError, setFetchError] = useState<string | null>(null);

  // Fetch state on mount from live API
  const loadTrades = async () => {
    setIsLoading(true);
    setFetchError(null);
    try {
      const apiTrades = await fetchTradesFromAPI();
      if (apiTrades && apiTrades.length > 0) {
        setTrades(apiTrades);
        setKpiStats(computeKPIStats(apiTrades));
        setIsApiConnected(true);
      } else {
        // Connected to API but table empty -> use mock trades for initial visual demo
        setIsApiConnected(true);
        setTrades(mockTrades);
        setKpiStats(computeKPIStats(mockTrades));
      }
    } catch (err: any) {
      console.warn('Backend API offline or unreachable, using initial mock dataset.');
      setIsApiConnected(false);
      setFetchError('Live API offline - using fallback dataset');
      setTrades(mockTrades);
      setKpiStats(computeKPIStats(mockTrades));
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    loadTrades();
  }, []);

  const handleCompleteTrade = async (tradeId: string, exitPrice: number, notes?: string) => {
    try {
      // Call PUT /api/trades/{id}
      let updatedTrade: Trade;
      if (isApiConnected) {
        updatedTrade = await completeTradeAPI(tradeId, { exit_price: exitPrice, notes });
      } else {
        // Fallback local update if API is disconnected
        const target = trades.find((t) => t.id === tradeId);
        updatedTrade = {
          ...(target || mockTrades[0]),
          id: tradeId,
          status: 'completed',
          exit_price: exitPrice,
          lessons_comments: notes || '',
        };
      }

      setTrades((prevTrades) => {
        const updatedList = prevTrades.map((t) => (t.id === tradeId ? updatedTrade : t));
        setKpiStats(computeKPIStats(updatedList));
        return updatedList;
      });
    } catch (err: any) {
      console.error('Failed to complete trade via API:', err);
      throw err;
    }
  };

  const pendingCount = trades.filter((t) => t.status === 'pending_review').length;

  return (
    <div className="min-h-screen bg-obsidian-bg text-slate-100 flex flex-col font-sans">
      {/* Navigation Topbar */}
      <Header pendingCount={pendingCount} onSyncComplete={loadTrades} />

      {/* Main Dashboard Container */}
      <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-8">
        
        {/* Top KPI Stat Strip */}
        <section>
          <KPIStatStrip stats={{ ...kpiStats, pendingCount }} />
        </section>

        {/* Day-of-Week Heatmap Layout Grid */}
        <section>
          <DayOfWeekHeatmap data={mockHeatmapData} />
        </section>

        {/* Trade Inbox Queue */}
        <section>
          <TradeInbox trades={trades} onCompleteTrade={handleCompleteTrade} />
        </section>

      </main>

      {/* Footer */}
      <footer className="border-t border-obsidian-border py-4 text-center text-xs text-obsidian-muted">
        Trading Journal Platform &bull; FastAPI Backend at http://localhost:8000 &bull; Obsidian Engine
      </footer>
    </div>
  );
}

import React from 'react';
import type { KPIStats } from '@/types/api';
import { TrendingUp, TrendingDown, Target, BarChart2, DollarSign, Clock, ShieldAlert } from 'lucide-react';

interface KPIStatStripProps {
  stats: KPIStats;
}

export const KPIStatStrip: React.FC<KPIStatStripProps> = ({ stats }) => {
  const isNetWin = stats.netPnl >= 0;

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-6 gap-4">
      
      {/* Net PnL */}
      <div className={`p-4 rounded-xl border bg-obsidian-card transition-all duration-200 ${
        isNetWin 
          ? 'border-win/30 shadow-win-glow hover:border-win/50' 
          : 'border-loss/30 shadow-loss-glow hover:border-loss/50'
      }`}>
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-obsidian-muted uppercase tracking-wider">Net P&L</span>
          <div className={`p-1.5 rounded-lg ${isNetWin ? 'bg-win/10 text-win' : 'bg-loss/10 text-loss'}`}>
            {isNetWin ? <TrendingUp className="h-4 w-4" /> : <TrendingDown className="h-4 w-4" />}
          </div>
        </div>
        <div className="mt-2 flex items-baseline justify-between">
          <span className={`text-2xl font-bold font-mono ${isNetWin ? 'text-win' : 'text-loss'}`}>
            {isNetWin ? '+' : ''}${stats.netPnl.toLocaleString('en-US', { minimumFractionDigits: 2 })}
          </span>
        </div>
        <div className="mt-2 flex items-center text-[11px] text-obsidian-muted">
          <span>Trailing 30 Days</span>
        </div>
      </div>

      {/* Win Rate */}
      <div className="p-4 rounded-xl border border-obsidian-border bg-obsidian-card hover:border-slate-700 transition-all duration-200">
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-obsidian-muted uppercase tracking-wider">Win Rate</span>
          <div className="p-1.5 rounded-lg bg-emerald-500/10 text-emerald-400">
            <Target className="h-4 w-4" />
          </div>
        </div>
        <div className="mt-2 flex items-baseline justify-between">
          <span className="text-2xl font-bold font-mono text-white">{stats.winRate}%</span>
        </div>
        <div className="mt-2 w-full bg-obsidian-bg rounded-full h-1.5 overflow-hidden border border-obsidian-border">
          <div 
            className="bg-win h-full rounded-full transition-all duration-500" 
            style={{ width: `${Math.min(stats.winRate, 100)}%` }}
          />
        </div>
      </div>

      {/* Total Trades */}
      <div className="p-4 rounded-xl border border-obsidian-border bg-obsidian-card hover:border-slate-700 transition-all duration-200">
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-obsidian-muted uppercase tracking-wider">Total Trades</span>
          <div className="p-1.5 rounded-lg bg-indigo-500/10 text-indigo-400">
            <BarChart2 className="h-4 w-4" />
          </div>
        </div>
        <div className="mt-2 flex items-baseline justify-between">
          <span className="text-2xl font-bold font-mono text-white">{stats.totalTrades}</span>
        </div>
        <div className="mt-2 text-[11px] text-obsidian-muted">
          <span>Sample Size</span>
        </div>
      </div>

      {/* Profit Factor */}
      <div className="p-4 rounded-xl border border-obsidian-border bg-obsidian-card hover:border-slate-700 transition-all duration-200">
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-obsidian-muted uppercase tracking-wider">Profit Factor</span>
          <div className="p-1.5 rounded-lg bg-cyan-500/10 text-cyan-400">
            <DollarSign className="h-4 w-4" />
          </div>
        </div>
        <div className="mt-2 flex items-baseline justify-between">
          <span className="text-2xl font-bold font-mono text-white">
            {stats.profitFactor === null ? '∞' : stats.profitFactor.toFixed(2)}
          </span>
        </div>
        <div className="mt-2 text-[11px] text-emerald-400 font-medium">
          <span>&gt; 2.0 Benchmark</span>
        </div>
      </div>

      {/* Avg ROI */}
      <div className="p-4 rounded-xl border border-obsidian-border bg-obsidian-card hover:border-slate-700 transition-all duration-200">
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-obsidian-muted uppercase tracking-wider">Avg Trade ROI</span>
          <div className="p-1.5 rounded-lg bg-purple-500/10 text-purple-400">
            <TrendingUp className="h-4 w-4" />
          </div>
        </div>
        <div className="mt-2 flex items-baseline justify-between">
          <span className="text-2xl font-bold font-mono text-win">+{stats.avgRoi}%</span>
        </div>
        <div className="mt-2 text-[11px] text-obsidian-muted">
          <span>Per Execution</span>
        </div>
      </div>

      {/* Pending Review Queue Alert */}
      <div className="p-4 rounded-xl border border-amber-500/30 bg-amber-950/10 hover:border-amber-500/50 transition-all duration-200 relative overflow-hidden group">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold text-amber-400 uppercase tracking-wider">Inbox Queue</span>
          <div className="p-1.5 rounded-lg bg-amber-500/20 text-amber-300">
            <Clock className="h-4 w-4" />
          </div>
        </div>
        <div className="mt-2 flex items-baseline justify-between">
          <span className="text-2xl font-bold font-mono text-amber-300">{stats.pendingCount}</span>
          <span className="text-xs text-amber-400/80 font-medium">Action Req.</span>
        </div>
        <div className="mt-2 flex items-center text-[11px] text-amber-400/70 space-x-1">
          <ShieldAlert className="h-3 w-3" />
          <span>Requires Manual Review</span>
        </div>
      </div>

    </div>
  );
};

'use client';

import React, { useState } from 'react';
import Link from 'next/link';
import { Activity, BookOpen, ShieldCheck, Zap, RefreshCw, SlidersHorizontal, User, Plus } from 'lucide-react';
import { SyncBrokerButton } from './SyncBrokerButton';
import { ManualTradeModal } from './ManualTradeModal';

interface HeaderProps {
  pendingCount: number;
  /** Reloads the dashboard trade list after a successful broker sync. */
  onSyncComplete?: () => void | Promise<void>;
}

export const Header: React.FC<HeaderProps> = ({ pendingCount, onSyncComplete }) => {
  const [isManualLogOpen, setIsManualLogOpen] = useState(false);

  return (
    <header className="border-b border-obsidian-border bg-obsidian-card/80 backdrop-blur-md sticky top-0 z-50">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
        
        {/* Left Branding */}
        <div className="flex items-center space-x-3">
          <div className="h-9 w-9 rounded-xl bg-gradient-to-br from-win/20 to-emerald-900/40 border border-win/30 flex items-center justify-center shadow-win-glow">
            <Activity className="h-5 w-5 text-win" />
          </div>
          <div>
            <div className="flex items-center space-x-2">
              <span className="font-bold text-lg tracking-wider text-white">TRADING JOURNAL</span>
              <span className="text-[10px] uppercase font-mono px-2 py-0.5 rounded bg-win/10 text-win border border-win/20">PRO</span>
            </div>
            <p className="text-xs text-obsidian-muted font-medium">IBKR Gateway Execution Engine</p>
          </div>
        </div>

        {/* Center Indicators */}
        <div className="hidden md:flex items-center space-x-6">
          <div className="flex items-center space-x-2 px-3 py-1.5 rounded-lg bg-obsidian-bg border border-obsidian-border text-xs font-mono">
            <span className="relative flex h-2 w-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-win opacity-75"></span>
              <span className="relative inline-flex rounded-full h-2 w-2 bg-win"></span>
            </span>
            <span className="text-slate-300">IBKR Sync:</span>
            <span className="text-win font-semibold">CONNECTED</span>
          </div>

          <div className="flex items-center space-x-2 px-3 py-1.5 rounded-lg bg-obsidian-bg border border-obsidian-border text-xs">
            <Zap className="h-3.5 w-3.5 text-amber-400" />
            <span className="text-obsidian-muted">Regime:</span>
            <span className="text-slate-200 font-medium">Bull Trending</span>
          </div>

          {pendingCount > 0 && (
            <div className="flex items-center space-x-2 px-3 py-1.5 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-400 text-xs font-mono animate-pulse">
              <RefreshCw className="h-3.5 w-3.5 animate-spin" />
              <span>{pendingCount} Pending Reviews</span>
            </div>
          )}
        </div>

        {/* Right Controls */}
        <div className="flex items-center space-x-3">
          <Link
            href="/strategies"
            className="inline-flex items-center gap-2 rounded-lg bg-obsidian-bg border border-obsidian-border px-3 py-2 text-xs font-medium text-obsidian-muted hover:text-slate-100 hover:border-slate-600 transition-colors"
          >
            <BookOpen className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">Strategies</span>
          </Link>

          <button
            type="button"
            onClick={() => setIsManualLogOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-lg bg-obsidian-bg border border-slate-700 px-3 py-2 text-xs font-medium text-slate-300 hover:text-white hover:border-slate-500 hover:bg-white/[0.04] transition-colors"
          >
            <Plus className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">Manual Log</span>
          </button>

          <SyncBrokerButton onSyncComplete={onSyncComplete} />

          <button className="p-2 rounded-lg bg-obsidian-bg border border-obsidian-border text-obsidian-muted hover:text-slate-200 hover:border-slate-700 transition">
            <SlidersHorizontal className="h-4 w-4" />
          </button>
          
          <div className="h-8 w-8 rounded-lg bg-slate-800 border border-obsidian-border flex items-center justify-center text-slate-300 font-semibold text-xs">
            <User className="h-4 w-4 text-slate-400" />
          </div>
        </div>

      </div>

      <ManualTradeModal
        open={isManualLogOpen}
        onClose={() => setIsManualLogOpen(false)}
      />
    </header>
  );
};

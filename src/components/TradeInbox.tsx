import React, { useState } from 'react';
import { Trade } from '@/types/trade';
import { Inbox, CheckCircle2, Clock, ShieldCheck, ArrowUpRight, ArrowDownRight, Edit3, Check, Loader2, AlertCircle } from 'lucide-react';

interface TradeInboxProps {
  trades: Trade[];
  onCompleteTrade: (tradeId: string, exitPrice: number, notes?: string) => Promise<void> | void;
}

export const TradeInbox: React.FC<TradeInboxProps> = ({ trades, onCompleteTrade }) => {
  const [activeTab, setActiveTab] = useState<'all' | 'pending_review' | 'completed'>('all');
  const [reviewingTradeId, setReviewingTradeId] = useState<string | null>(null);
  const [exitPriceInput, setExitPriceInput] = useState<string>('');
  const [notesInput, setNotesInput] = useState<string>('');
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const filteredTrades = trades.filter((trade) => {
    if (activeTab === 'pending_review') return trade.status === 'pending_review';
    if (activeTab === 'completed') return trade.status === 'completed';
    return true;
  });

  const handleStartReview = (trade: Trade) => {
    setReviewingTradeId(trade.id);
    setExitPriceInput(trade.exit_price ? trade.exit_price.toString() : trade.actual_entry.toString());
    setNotesInput(trade.lessons_comments || '');
    setErrorMessage(null);
  };

  const handleSaveReview = async (tradeId: string) => {
    const parsedExit = parseFloat(exitPriceInput);
    if (isNaN(parsedExit) || parsedExit <= 0) {
      setErrorMessage('Please enter a valid positive exit price.');
      return;
    }

    setIsSubmitting(true);
    setErrorMessage(null);

    try {
      await onCompleteTrade(tradeId, parsedExit, notesInput);
      setReviewingTradeId(null);
    } catch (err: any) {
      setErrorMessage(err.message || 'Failed to update trade via API.');
    } finally {
      setIsSubmitting(false);
    }
  };

  const calculatePnl = (trade: Trade) => {
    if (trade.exit_price === null || trade.exit_price === undefined) return null;
    const diff = trade.direction === 'LONG' ? trade.exit_price - trade.actual_entry : trade.actual_entry - trade.exit_price;
    return diff * trade.quantity;
  };

  return (
    <div className="p-5 rounded-xl border border-obsidian-border bg-obsidian-card">
      
      {/* Inbox Bar */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between pb-4 border-b border-obsidian-border gap-3">
        <div className="flex items-center space-x-3">
          <div className="p-2 rounded-lg bg-indigo-500/10 text-indigo-400">
            <Inbox className="h-5 w-5" />
          </div>
          <div>
            <h2 className="text-base font-semibold text-white">Trade Inbox Queue</h2>
            <p className="text-xs text-obsidian-muted">Review executions & validate plan compliance (Live API Connected)</p>
          </div>
        </div>

        {/* Status Filter Tabs */}
        <div className="flex items-center space-x-1 p-1 bg-obsidian-bg rounded-lg border border-obsidian-border text-xs">
          <button
            onClick={() => setActiveTab('all')}
            className={`px-3 py-1.5 rounded-md font-medium transition ${
              activeTab === 'all'
                ? 'bg-obsidian-card text-white border border-slate-700'
                : 'text-obsidian-muted hover:text-slate-200'
            }`}
          >
            All ({trades.length})
          </button>
          <button
            onClick={() => setActiveTab('pending_review')}
            className={`px-3 py-1.5 rounded-md font-medium transition flex items-center space-x-1.5 ${
              activeTab === 'pending_review'
                ? 'bg-amber-500/20 text-amber-300 border border-amber-500/30'
                : 'text-obsidian-muted hover:text-slate-200'
            }`}
          >
            <Clock className="h-3.5 w-3.5 text-amber-400" />
            <span>Pending ({trades.filter((t) => t.status === 'pending_review').length})</span>
          </button>
          <button
            onClick={() => setActiveTab('completed')}
            className={`px-3 py-1.5 rounded-md font-medium transition flex items-center space-x-1.5 ${
              activeTab === 'completed'
                ? 'bg-win/20 text-win border border-win/30'
                : 'text-obsidian-muted hover:text-slate-200'
            }`}
          >
            <CheckCircle2 className="h-3.5 w-3.5 text-win" />
            <span>Completed ({trades.filter((t) => t.status === 'completed').length})</span>
          </button>
        </div>
      </div>

      {/* Trade Queue List */}
      <div className="mt-4 space-y-3">
        {filteredTrades.length === 0 ? (
          <div className="p-8 text-center border border-dashed border-obsidian-border rounded-lg text-obsidian-muted text-sm">
            No trades match the selected filter tab.
          </div>
        ) : (
          filteredTrades.map((trade) => {
            const pnl = calculatePnl(trade);
            const isWin = pnl !== null && pnl >= 0;
            const isPending = trade.status === 'pending_review';
            const isEditing = reviewingTradeId === trade.id;

            return (
              <div
                key={trade.id}
                className={`p-4 rounded-xl border transition-all ${
                  isPending
                    ? 'bg-obsidian-bg/80 border-amber-500/30 hover:border-amber-500/50'
                    : 'bg-obsidian-bg/40 border-obsidian-border hover:border-slate-700'
                }`}
              >
                <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4">
                  
                  {/* Symbol & Direction Details */}
                  <div className="flex items-center space-x-4">
                    <div
                      className={`h-11 w-11 rounded-xl flex items-center justify-center font-bold text-sm ${
                        trade.direction === 'LONG'
                          ? 'bg-win/10 text-win border border-win/30'
                          : 'bg-loss/10 text-loss border border-loss/30'
                      }`}
                    >
                      {trade.direction === 'LONG' ? (
                        <ArrowUpRight className="h-5 w-5" />
                      ) : (
                        <ArrowDownRight className="h-5 w-5" />
                      )}
                    </div>

                    <div>
                      <div className="flex items-center space-x-2">
                        <span className="font-mono font-bold text-lg text-white">{trade.ticker}</span>
                        <span
                          className={`text-[10px] font-mono uppercase px-2 py-0.5 rounded font-semibold ${
                            trade.direction === 'LONG'
                              ? 'bg-win/10 text-win border border-win/20'
                              : 'bg-loss/10 text-loss border border-loss/20'
                          }`}
                        >
                          {trade.direction}
                        </span>
                        <span className="text-xs text-obsidian-muted bg-obsidian-card px-2 py-0.5 rounded border border-obsidian-border">
                          #{trade.id}
                        </span>
                      </div>
                      <div className="text-xs text-obsidian-muted font-mono mt-0.5">
                        Entry: ${trade.actual_entry.toFixed(2)} &bull; Qty: {trade.quantity} &bull; Source: {trade.source_tag}
                      </div>
                    </div>
                  </div>

                  {/* Discipline Checkboxes */}
                  <div className="flex items-center space-x-4 text-xs">
                    <div className="flex items-center space-x-1">
                      <ShieldCheck className={`h-4 w-4 ${trade.hard_sl_set ? 'text-win' : 'text-slate-600'}`} />
                      <span className={trade.hard_sl_set ? 'text-slate-300' : 'text-slate-500'}>Hard SL</span>
                    </div>
                    <div className="flex items-center space-x-1">
                      <CheckCircle2 className={`h-4 w-4 ${trade.waited_retest ? 'text-win' : 'text-slate-600'}`} />
                      <span className={trade.waited_retest ? 'text-slate-300' : 'text-slate-500'}>Retest</span>
                    </div>
                    <div className="flex items-center space-x-1">
                      <CheckCircle2 className={`h-4 w-4 ${trade.followed_plan ? 'text-win' : 'text-slate-600'}`} />
                      <span className={trade.followed_plan ? 'text-slate-300' : 'text-slate-500'}>Plan</span>
                    </div>

                    {trade.grade && (
                      <span className="ml-2 font-mono text-xs px-2 py-0.5 rounded bg-indigo-500/10 text-indigo-400 border border-indigo-500/20 font-bold">
                        Grade {trade.grade}
                      </span>
                    )}
                  </div>

                  {/* PnL & Review Controls */}
                  <div className="flex items-center space-x-4">
                    {pnl !== null ? (
                      <div className="text-right">
                        <div className={`text-base font-mono font-bold ${isWin ? 'text-win' : 'text-loss'}`}>
                          {isWin ? '+' : ''}${pnl.toLocaleString('en-US', { minimumFractionDigits: 2 })}
                        </div>
                        <div className="text-[11px] text-obsidian-muted font-mono">
                          Exit: ${trade.exit_price?.toFixed(2)}
                        </div>
                      </div>
                    ) : (
                      <div className="text-right text-xs text-amber-400 font-mono">
                        Awaiting Exit
                      </div>
                    )}

                    {isPending ? (
                      <button
                        onClick={() => handleStartReview(trade)}
                        className="px-3 py-1.5 rounded-lg bg-amber-500 text-obsidian-bg font-semibold text-xs flex items-center space-x-1 hover:bg-amber-400 transition"
                      >
                        <Edit3 className="h-3.5 w-3.5" />
                        <span>Complete Review</span>
                      </button>
                    ) : (
                      <span className="px-2.5 py-1 rounded-lg bg-emerald-500/10 text-win text-xs border border-win/20 font-medium flex items-center space-x-1">
                        <Check className="h-3.5 w-3.5" />
                        <span>Completed</span>
                      </span>
                    )}
                  </div>

                </div>

                {/* Inline Review Form */}
                {isEditing && (
                  <div className="mt-4 p-3 rounded-lg bg-obsidian-card border border-amber-500/40 space-y-3">
                    <div className="text-xs font-semibold text-amber-400">
                      PUT /api/trades/{trade.id} &bull; Complete Review
                    </div>

                    {errorMessage && (
                      <div className="p-2 rounded bg-loss/10 border border-loss/30 text-loss text-xs flex items-center space-x-1.5">
                        <AlertCircle className="h-4 w-4 shrink-0" />
                        <span>{errorMessage}</span>
                      </div>
                    )}

                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                      <div>
                        <label className="text-[11px] text-obsidian-muted block mb-1">Exit Price ($) *</label>
                        <input
                          type="number"
                          step="0.01"
                          value={exitPriceInput}
                          onChange={(e) => setExitPriceInput(e.target.value)}
                          disabled={isSubmitting}
                          className="w-full bg-obsidian-bg border border-obsidian-border rounded px-3 py-1.5 text-xs text-white font-mono focus:border-amber-400 focus:outline-none disabled:opacity-50"
                        />
                      </div>
                      <div>
                        <label className="text-[11px] text-obsidian-muted block mb-1">Notes / Lessons Learned</label>
                        <input
                          type="text"
                          value={notesInput}
                          onChange={(e) => setNotesInput(e.target.value)}
                          placeholder="e.g. Exit target hit, obeyed stop loss"
                          disabled={isSubmitting}
                          className="w-full bg-obsidian-bg border border-obsidian-border rounded px-3 py-1.5 text-xs text-white focus:border-amber-400 focus:outline-none disabled:opacity-50"
                        />
                      </div>
                    </div>

                    <div className="flex justify-end space-x-2">
                      <button
                        onClick={() => setReviewingTradeId(null)}
                        disabled={isSubmitting}
                        className="px-3 py-1 rounded bg-obsidian-bg border border-obsidian-border text-xs text-obsidian-muted hover:text-white disabled:opacity-50"
                      >
                        Cancel
                      </button>
                      <button
                        onClick={() => handleSaveReview(trade.id)}
                        disabled={isSubmitting}
                        className="px-3 py-1 rounded bg-win text-obsidian-bg font-semibold text-xs hover:bg-emerald-400 flex items-center space-x-1.5 disabled:opacity-50"
                      >
                        {isSubmitting ? (
                          <>
                            <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            <span>Updating API...</span>
                          </>
                        ) : (
                          <span>Submit PUT Request</span>
                        )}
                      </button>
                    </div>
                  </div>
                )}

              </div>
            );
          })
        )}
      </div>

    </div>
  );
};

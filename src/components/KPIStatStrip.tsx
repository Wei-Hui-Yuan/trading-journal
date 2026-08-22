import React from 'react';
import type { KPIStats } from '@/types/api';
import { formatMoney, formatSignedPercent } from '@/lib/format';
import { TrendingUp, TrendingDown, Target, Gauge } from 'lucide-react';

interface KPIStatStripProps {
  stats: KPIStats;
}

export const KPIStatStrip: React.FC<KPIStatStripProps> = ({ stats }) => {
  const isNetWin = stats.netPnl >= 0;
  const isRoiPositive = stats.avgRoi > 0;

  // lg:grid-cols-3: three cards, one clean row -- the column count survives
  // unchanged from the six-card layout, since three of six divides exactly
  // as cleanly as three of three. Win Rate and Total Trades merged into one
  // card below (a bare trade count was context for the win rate, not a
  // fact worth a whole card), and Profit Factor/Avg ROI/Avg R merged into
  // "Trade Quality" -- CSS grid stretches every card in a row to match the
  // tallest, and with six cards that meant Win Rate and Total Trades sat at
  // 43% and 38% dead space next to Net P&L's five sub-rows (measured
  // in-browser). Real multi-line content in their place closes most of
  // that gap without changing the grid at all.
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
      
      {/* Net PnL */}
      <div className={`p-4 rounded-xl border bg-obsidian-card transition-all duration-200 ${
        isNetWin 
          ? 'border-win/30 shadow-win-glow hover:border-win/50' 
          : 'border-loss/30 shadow-loss-glow hover:border-loss/50'
      }`}>
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-obsidian-muted uppercase tracking-wider">Net P&amp;L</span>
          <div className={`p-1.5 rounded-lg ${isNetWin ? 'bg-win/10 text-win' : 'bg-loss/10 text-loss'}`}>
            {isNetWin ? <TrendingUp className="h-4 w-4" /> : <TrendingDown className="h-4 w-4" />}
          </div>
        </div>
        <div className="mt-2 flex items-baseline justify-between">
          <span className={`text-2xl font-bold font-mono ${isNetWin ? 'text-win' : 'text-loss'}`}>
            {formatMoney(stats.netPnl)}
          </span>
        </div>
        
        {/* Sub-breakdown: Market Trading P&L vs Broker Fees */}
        <div className="mt-3 pt-2.5 border-t border-obsidian-border/60 flex flex-col space-y-1 font-mono">
          <div className="flex justify-between items-center text-xs">
            <span className="text-[11px] font-semibold text-slate-300">Market Trading P&amp;L:</span>
            <span className={stats.grossPnl >= 0 ? 'text-win font-semibold' : 'text-loss font-semibold'}>
              {formatMoney(stats.grossPnl)}
            </span>
          </div>
          <div className="flex justify-between items-center text-xs">
            {/* "All-in", not "commissions", because that is what it is. The
                figure is derived from IBKR's own realised P&L rather than from
                the commission column, so it also carries the exchange,
                clearing and regulatory charges IBKR nets but does not report
                separately — about 5.3c per closing fill. That is precisely
                what makes the three lines here sum to the statement. */}
            <span
              className="text-[11px] font-semibold text-slate-300 cursor-help border-b border-dotted border-obsidian-border"
              title={
                `IBKR commission: ${formatMoney(-stats.ibCommission)}\n` +
                `Exchange, clearing & regulatory: ${formatMoney(
                  -(stats.totalCommission - stats.ibCommission)
                )}\n\n` +
                'Derived from the broker’s own realised P&L, so Net P&L ' +
                'matches your IBKR statement exactly.' +
                (stats.unverifiedLegs > 0
                  ? `\n\nNote: ${stats.unverifiedLegs} slice(s) had no broker figure, ` +
                    'so their cost is the commission alone.'
                  : '')
              }
            >
              Broker Fees &amp; Commissions:
              {stats.unverifiedLegs > 0 && (
                <span className="ml-1 text-amber-500" aria-hidden>*</span>
              )}
            </span>
            {/* Negated INSIDE formatMoney, not prefixed outside it. The
                formatter already signs its own output, so a literal `-` in
                front produced `-+$97.65` — the same mistake formatSignedPercent
                documents as having produced `+-0.63%`.

                Commission is stored as a cost, so a positive figure is money
                paid and reads here as negative. Negating first also gets the
                two edge cases right for free: zero renders `$0.00` rather than
                `-$0.00`, and a net rebate renders `+$12.50` rather than
                `--$12.50`. */}
            <span className="text-amber-400 font-semibold">
              {formatMoney(-stats.totalCommission)}
            </span>
          </div>
          {/* Only when there is some, because most windows have none.

              This is money already banked out of positions still open, and it
              is the reason Net P&L is not the sum of the closed round trips
              below. It used to be missing from every figure in the app --
              MSFT alone was carrying $63 of realised losses that no total
              could see -- so naming it is the point, not a detail. */}
          {stats.openRunPnl !== 0 && (
            <div className="flex justify-between items-center text-xs">
              <span
                className="text-[11px] font-semibold text-slate-300 cursor-help border-b border-dotted border-obsidian-border"
                title={
                  'Realised by scaling out of positions you still hold. Already ' +
                  'in Net P&L, but not in the closed round trips below — those ' +
                  'trades have not finished, so they do not count toward win ' +
                  'rate or trade count.'
                }
              >
                From open positions:
              </span>
              <span className={stats.openRunPnl >= 0 ? 'text-win font-semibold' : 'text-loss font-semibold'}>
                {formatMoney(stats.openRunPnl)}
              </span>
            </div>
          )}
        </div>
      </div>

      {/* Win Rate + Total Trades. Total Trades used to be its own card, but a
          bare count next to Win Rate had nothing else to say -- it exists to
          answer "how much sits behind that percentage", which is exactly
          what a win rate's own sample size is. Folded in as the qualifier
          line rather than kept as a headline of its own. */}
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
        <div className="mt-2 text-[11px] text-obsidian-muted">
          <span>{stats.totalTrades} trade{stats.totalTrades === 1 ? '' : 's'}</span>
        </div>

        {/* The population behind the percentage, in Net P&L's breakdown
            vocabulary rather than a new one -- same divider, same row shape,
            same 11px muted label against a coloured figure.

            Rendered only when the API reports it. `wins === null` means an
            older backend that never counted, which is a different fact from
            a window that genuinely had none, and inventing "0 W" for it
            would be a lie the card cannot take back. */}
        {stats.wins !== null && stats.losses !== null && (
          <div className="mt-3 pt-2.5 border-t border-obsidian-border/60 flex flex-col space-y-1 font-mono">
            <div className="flex justify-between items-center text-xs">
              <span className="text-[11px] font-semibold text-slate-300">Wins:</span>
              <span className="text-win font-semibold">{stats.wins}</span>
            </div>
            <div className="flex justify-between items-center text-xs">
              <span className="text-[11px] font-semibold text-slate-300">Losses:</span>
              <span className="text-loss font-semibold">{stats.losses}</span>
            </div>
            {/* Only when there are some, exactly like Net P&L's open-positions
                row. A scratch closed at precisely break-even, so it is neither
                a win nor a loss -- but win_rate_pct divides by ALL trades, so
                it still drags the percentage down. Naming it is what stops
                "47 + 87 does not make 136" from reading as a bug. */}
            {stats.scratches !== null && stats.scratches > 0 && (
              <div className="flex justify-between items-center text-xs">
                <span
                  className="text-[11px] font-semibold text-slate-300 cursor-help border-b border-dotted border-obsidian-border"
                  title={
                    'Closed at exactly break-even, so neither a win nor a loss. ' +
                    'Still counted in the ' + stats.totalTrades + ' above, and still ' +
                    'in the win rate’s denominator.'
                  }
                >
                  Scratches:
                </span>
                <span className="text-slate-300 font-semibold">{stats.scratches}</span>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Trade Quality: Profit Factor, Avg ROI, and Avg R merged into one
          card. The three used to be separate cards, each stretched to Net
          P&L's height by CSS grid with almost nothing to fill it -- the same
          problem Win Rate/Total Trades had. They share no common
          denominator (gross $ ratio, capital-weighted %, and a mean over
          scored trades are three different things), so each row keeps its
          own qualifier immediately below it rather than one heading
          implying they agree. */}
      <div className="p-4 rounded-xl border border-obsidian-border bg-obsidian-card hover:border-slate-700 transition-all duration-200">
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-obsidian-muted uppercase tracking-wider">Trade Quality</span>
          <div className="p-1.5 rounded-lg bg-cyan-500/10 text-cyan-400">
            <Gauge className="h-4 w-4" />
          </div>
        </div>

        <div className="mt-1.5 space-y-1">
          <div>
            <div className="flex items-baseline justify-between">
              {/* Suffixed because Analytics shows a DIFFERENT profit factor,
                  computed on R-multiples -- this one is gross win $ / loss $. */}
              <span className="text-[11px] font-semibold text-slate-300">Profit Factor ($)</span>
              <span className="font-mono text-base font-semibold text-white">
                {stats.profitFactor === null ? '∞' : stats.profitFactor.toFixed(2)}
              </span>
            </div>
            <p className="text-[10px] text-obsidian-muted">Gross win $ / loss $</p>
          </div>

          <div className="border-t border-obsidian-border/60 pt-1">
            <div className="flex items-baseline justify-between">
              <span className="text-[11px] font-semibold text-slate-300">Avg Trade ROI</span>
              {/* Coloured by its own sign, not hardcoded green. A losing
                  average rendered in win-green alongside a "+" it had not
                  earned. */}
              <span
                className={`font-mono text-base font-semibold ${
                  isRoiPositive ? 'text-win' : stats.avgRoi < 0 ? 'text-loss' : 'text-white'
                }`}
              >
                {formatSignedPercent(stats.avgRoi)}
              </span>
            </div>
            {/* Was "Per Execution" -- wrong. `avg_roi_pct` sums P&L and cost
                basis across whole POSITIONS (round trips), not individual
                fills, and divides once: capital-weighted, not a plain mean
                of each trade's own ROI%. */}
            <p className="text-[10px] text-obsidian-muted">Capital-weighted</p>
          </div>

          <div className="border-t border-obsidian-border/60 pt-1">
            <div className="flex items-baseline justify-between">
              {/* A different denominator than the ROI above -- this is
                  Analytics' `avg_r`, from the trades ledger, not
                  `core_stats`. null means no trade in the window has both an
                  exit and a usable stop to score, which most journals will
                  see before their first stop is entered. */}
              <span className="text-[11px] font-semibold text-slate-300">Avg R</span>
              <span
                className={`font-mono text-base font-semibold ${
                  stats.avgR === null
                    ? 'text-white'
                    : stats.avgR > 0
                      ? 'text-win'
                      : stats.avgR < 0
                        ? 'text-loss'
                        : 'text-white'
                }`}
              >
                {stats.avgR === null
                  ? '—'
                  : `${stats.avgR >= 0 ? '+' : ''}${stats.avgR.toFixed(2)}R`}
              </span>
            </div>
            <p className="text-[10px] text-obsidian-muted">
              {stats.avgRSample} scored trade{stats.avgRSample === 1 ? '' : 's'}
            </p>
          </div>
        </div>
      </div>

    </div>
  );
};

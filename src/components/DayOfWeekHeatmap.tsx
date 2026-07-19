import React, { useState } from 'react';
import { HeatmapCell } from '@/types/trade';
import { Calendar, Info } from 'lucide-react';

interface DayOfWeekHeatmapProps {
  data: HeatmapCell[];
}

const DAYS = [
  { id: 1, label: 'Mon', fullLabel: 'Monday' },
  { id: 2, label: 'Tue', fullLabel: 'Tuesday' },
  { id: 3, label: 'Wed', fullLabel: 'Wednesday' },
  { id: 4, label: 'Thu', fullLabel: 'Thursday' },
  { id: 5, label: 'Fri', fullLabel: 'Friday' },
];

const TIME_SLOTS: Array<'Morning' | 'Midday' | 'Afternoon' | 'After-Hours'> = [
  'Morning',
  'Midday',
  'Afternoon',
  'After-Hours',
];

export const DayOfWeekHeatmap: React.FC<DayOfWeekHeatmapProps> = ({ data }) => {
  const [hoveredCell, setHoveredCell] = useState<{ day: number; time: string } | null>(null);

  const getCellData = (dayOfWeek: number, timeSlot: string) => {
    return data.find((d) => d.dayOfWeek === dayOfWeek && d.timeSlot === timeSlot) || {
      dayOfWeek,
      timeSlot,
      pnl: 0,
      tradeCount: 0,
    };
  };

  const getCellColorClass = (pnl: number, tradeCount: number) => {
    if (tradeCount === 0) {
      return 'bg-obsidian-bg/60 text-obsidian-muted border-obsidian-border/50 hover:border-slate-600';
    }
    if (pnl > 1000) {
      return 'bg-win/20 text-win border-win/40 shadow-win-glow hover:border-win font-semibold';
    }
    if (pnl > 0) {
      return 'bg-emerald-950/40 text-emerald-400 border-emerald-800/50 hover:border-emerald-500 font-medium';
    }
    return 'bg-loss/20 text-loss border-loss/40 shadow-loss-glow hover:border-loss font-semibold';
  };

  return (
    <div className="p-5 rounded-xl border border-obsidian-border bg-obsidian-card">
      
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between pb-4 border-b border-obsidian-border gap-2">
        <div className="flex items-center space-x-2">
          <Calendar className="h-5 w-5 text-win" />
          <h2 className="text-base font-semibold text-white">Day-of-Week Performance Heatmap</h2>
        </div>
        <div className="flex items-center space-x-4 text-xs">
          <div className="flex items-center space-x-1.5">
            <span className="h-3 w-3 rounded bg-win/20 border border-win/40 inline-block"></span>
            <span className="text-obsidian-muted">High PnL</span>
          </div>
          <div className="flex items-center space-x-1.5">
            <span className="h-3 w-3 rounded bg-emerald-950/40 border border-emerald-800/50 inline-block"></span>
            <span className="text-obsidian-muted">Mod Win</span>
          </div>
          <div className="flex items-center space-x-1.5">
            <span className="h-3 w-3 rounded bg-loss/20 border border-loss/40 inline-block"></span>
            <span className="text-obsidian-muted">Loss</span>
          </div>
        </div>
      </div>

      {/* Heatmap Grid Layout */}
      <div className="mt-4 overflow-x-auto">
        <div className="min-w-[640px]">
          
          {/* Time Slot Headers */}
          <div className="grid grid-cols-5 gap-2 mb-2 pl-20 text-center text-xs font-mono text-obsidian-muted uppercase tracking-wider">
            {TIME_SLOTS.map((slot) => (
              <div key={slot} className="py-1 bg-obsidian-bg/40 rounded border border-obsidian-border/30">
                {slot}
              </div>
            ))}
          </div>

          {/* Grid Rows per Day */}
          {DAYS.map((day) => {
            const dayCells = TIME_SLOTS.map((slot) => getCellData(day.id, slot));
            const dayTotalPnl = dayCells.reduce((acc, cell) => acc + cell.pnl, 0);

            return (
              <div key={day.id} className="grid grid-cols-5 gap-2 mb-2 items-center">
                {/* Day Header Label */}
                <div className="w-18 pr-2 flex flex-col justify-center">
                  <span className="text-xs font-bold text-slate-200">{day.fullLabel}</span>
                  <span className={`text-[10px] font-mono ${dayTotalPnl >= 0 ? 'text-win' : 'text-loss'}`}>
                    {dayTotalPnl >= 0 ? '+' : ''}${dayTotalPnl.toLocaleString()}
                  </span>
                </div>

                {/* Cells for Time Slots */}
                {TIME_SLOTS.map((slot) => {
                  const cell = getCellData(day.id, slot);
                  const isHovered = hoveredCell?.day === day.id && hoveredCell?.time === slot;

                  return (
                    <div
                      key={slot}
                      onMouseEnter={() => setHoveredCell({ day: day.id, time: slot })}
                      onMouseLeave={() => setHoveredCell(null)}
                      className={`relative p-3 rounded-lg border text-center transition-all duration-150 cursor-pointer ${getCellColorClass(
                        cell.pnl,
                        cell.tradeCount
                      )}`}
                    >
                      <div className="text-sm font-mono">
                        {cell.tradeCount > 0 ? (
                          <span>
                            {cell.pnl >= 0 ? '+' : ''}${cell.pnl.toLocaleString()}
                          </span>
                        ) : (
                          <span className="opacity-40">-</span>
                        )}
                      </div>
                      <div className="text-[10px] font-mono opacity-75 mt-0.5">
                        {cell.tradeCount > 0 ? `${cell.tradeCount} trade${cell.tradeCount > 1 ? 's' : ''}` : 'No trades'}
                      </div>

                      {/* Tooltip Overlay */}
                      {isHovered && cell.tradeCount > 0 && (
                        <div className="absolute z-20 bottom-full left-1/2 transform -translate-x-1/2 mb-2 px-3 py-2 bg-obsidian-card border border-slate-700 rounded-lg shadow-xl text-xs whitespace-nowrap text-left text-slate-200">
                          <div className="font-semibold text-white mb-1">
                            {day.fullLabel} &bull; {slot}
                          </div>
                          <div>PnL: <span className={cell.pnl >= 0 ? 'text-win font-bold' : 'text-loss font-bold'}>{cell.pnl >= 0 ? '+' : ''}${cell.pnl.toLocaleString()}</span></div>
                          <div>Trades: <span className="font-mono text-white">{cell.tradeCount}</span></div>
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

    </div>
  );
};

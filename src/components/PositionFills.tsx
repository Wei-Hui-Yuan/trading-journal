'use client';

import React from 'react';
import { Loader2 } from 'lucide-react';

import { usePositionFills } from '@/hooks/useTradeInbox';

interface PositionFillsProps {
  positionId: string;
  /** Gates the fetch: collapsed rows must not each fire a request. */
  open: boolean;
}

const timeFormatter = new Intl.DateTimeFormat('en-US', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
  timeZone: 'America/New_York',
});

/**
 * The executions behind one round trip.
 *
 * A position aggregates flat-to-flat, so its entry and exit are
 * quantity-weighted averages. This is what those averages hide: each scale-in
 * and scale-out, at the price it actually filled.
 */
export const PositionFills: React.FC<PositionFillsProps> = ({ positionId, open }) => {
  const { data: fills, isLoading, error } = usePositionFills(positionId, open);

  if (!open) return null;

  if (isLoading) {
    return (
      <div className="flex items-center space-x-2 px-3 py-2 text-[11px] text-obsidian-muted">
        <Loader2 className="h-3 w-3 animate-spin" />
        <span>Loading executions…</span>
      </div>
    );
  }

  if (error) {
    return (
      <p className="px-3 py-2 text-[11px] text-loss">
        {(error as Error).message || 'Could not load executions'}
      </p>
    );
  }

  if (!fills || fills.length === 0) {
    // Positions matched before migration 009 have no fills recorded.
    return (
      <p className="px-3 py-2 text-[11px] text-obsidian-muted">
        No execution detail recorded for this position.
      </p>
    );
  }

  return (
    <div className="rounded-lg border border-obsidian-border bg-obsidian-bg/60 overflow-x-auto">
      <table className="w-full text-[11px] font-mono">
        <thead>
          <tr className="text-obsidian-muted border-b border-obsidian-border">
            <th className="text-left font-medium px-3 py-1.5">Side</th>
            <th className="text-right font-medium px-3 py-1.5">Qty</th>
            <th className="text-right font-medium px-3 py-1.5">Price</th>
            <th className="text-right font-medium px-3 py-1.5">Time (ET)</th>
          </tr>
        </thead>
        <tbody>
          {fills.map((fill) => (
            <tr key={fill.id} className="border-b border-obsidian-border/50 last:border-0">
              <td className="px-3 py-1.5">
                <span
                  className={`px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wider ${
                    fill.role === 'OPEN'
                      ? 'bg-sky-500/10 text-sky-300'
                      : 'bg-amber-500/10 text-amber-300'
                  }`}
                >
                  {fill.role === 'OPEN' ? 'Entry' : 'Exit'}
                </span>
              </td>
              <td className="px-3 py-1.5 text-right text-slate-200">{fill.quantity}</td>
              <td className="px-3 py-1.5 text-right text-slate-200">{fill.price}</td>
              <td className="px-3 py-1.5 text-right text-obsidian-muted">
                {timeFormatter.format(new Date(fill.executed_at))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

export default PositionFills;

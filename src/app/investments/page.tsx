'use client';

import React from 'react';
import { InvestmentTable } from '@/components/InvestmentTable';

/**
 * The long-term book, kept apart from the trading journal on purpose.
 *
 * `positions` is built around an idea opened and closed — FIFO round trips,
 * R-multiples, entry slippage, a discipline checklist. None of that means
 * anything for a holding bought monthly and kept for a decade, which is
 * measured against what the business is worth rather than against a stop.
 * Two questions, two schemas, two pages.
 */
export default function InvestmentsPage() {
  return (
    <div className="min-h-screen bg-obsidian-bg">
      <main className="mx-auto max-w-[1400px] px-6 py-6">
        <InvestmentTable />
      </main>
    </div>
  );
}

'use client';

import React from 'react';
import { Landmark } from 'lucide-react';

import { PageHeader } from '@/components/PageHeader';
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
      <PageHeader
        icon={Landmark}
        title="INVESTMENT PORTFOLIO"
        subtitle="The long-term book — what you hold, and what the model says it is worth"
        accent="sky"
        // Wider than every other page, matching the table below it.
        maxWidthClass="max-w-[1400px]"
      />

      <main className="mx-auto max-w-[1400px] px-6 py-6">
        <InvestmentTable />
      </main>
    </div>
  );
}

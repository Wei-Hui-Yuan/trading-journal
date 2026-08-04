'use client';

import React from 'react';
import Link from 'next/link';
import { ArrowLeft, Landmark } from 'lucide-react';

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
      <header className="border-b border-obsidian-border bg-obsidian-card/60 backdrop-blur-md">
        <div className="mx-auto flex max-w-[1400px] items-center justify-between px-6 py-4">
          <div className="flex items-center space-x-3">
            <div className="rounded-lg border border-obsidian-border bg-obsidian-bg p-2 text-sky-400">
              <Landmark className="h-5 w-5" />
            </div>
            <div>
              <h1 className="text-lg font-bold tracking-tight text-slate-100">
                INVESTMENT PORTFOLIO
              </h1>
              <p className="text-[11px] text-obsidian-muted">
                The long-term book — what you hold, and what the model says it is worth
              </p>
            </div>
          </div>

          <Link
            href="/"
            className="inline-flex items-center space-x-1.5 rounded-lg border border-obsidian-border px-3 py-2 text-xs text-obsidian-muted transition-colors hover:border-slate-600 hover:text-slate-200"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            <span>Dashboard</span>
          </Link>
        </div>
      </header>

      <main className="mx-auto max-w-[1400px] px-6 py-6">
        <InvestmentTable />
      </main>
    </div>
  );
}

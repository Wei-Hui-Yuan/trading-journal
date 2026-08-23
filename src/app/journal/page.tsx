'use client';

import React from 'react';
import { BookText } from 'lucide-react';

import { PageHeader } from '@/components/PageHeader';
import { TradeLedger } from '@/components/TradeLedger';

/**
 * The master ledger, grouped by trade idea.
 *
 * Every other surface reads `positions`, which holds only closed round trips.
 * This is the one view that also shows open exposure — and the only one that
 * carries the plan and the post-mortem, neither of which a broker feed can
 * supply. It is deliberately the densest page in the app.
 */
export default function JournalPage() {
  return (
    <div className="min-h-screen bg-obsidian-bg">
      <PageHeader
        icon={BookText}
        title="TRADE JOURNAL"
        subtitle="Every trade, open and closed — the plan, the fills, and the review"
        accent="emerald"
      />

      <main className="mx-auto max-w-7xl px-6 py-6">
        <TradeLedger />
      </main>
    </div>
  );
}

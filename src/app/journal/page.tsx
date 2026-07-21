'use client';

import React from 'react';
import Link from 'next/link';
import { ArrowLeft, BookText } from 'lucide-react';

import { TradeLedger } from '@/components/TradeLedger';

/**
 * The master ledger.
 *
 * Every other surface reads `positions`, which holds only closed round trips.
 * This is the one view that shows open exposure — and the reasoning attached
 * to each execution, which the broker feed cannot supply.
 */
export default function JournalPage() {
  return (
    <div className="min-h-screen bg-obsidian-bg">
      <header className="border-b border-obsidian-border bg-obsidian-card/60 backdrop-blur-md">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-4">
          <div className="flex items-center space-x-3">
            <div className="rounded-lg border border-obsidian-border bg-obsidian-bg p-2 text-emerald-400">
              <BookText className="h-5 w-5" />
            </div>
            <div>
              <h1 className="text-lg font-bold tracking-tight text-slate-100">
                TRADE JOURNAL
              </h1>
              <p className="text-[11px] text-obsidian-muted">
                Every execution, open and closed — with the thinking behind it
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

      <main className="mx-auto max-w-7xl px-6 py-6">
        <TradeLedger />
      </main>
    </div>
  );
}

'use client';

import React from 'react';
import Link from 'next/link';
import { ArrowLeft, Calculator } from 'lucide-react';

import { SizingScratchpad } from '@/components/SizingScratchpad';

/**
 * A fast scratchpad for sizing a trade before there is time to write a real
 * plan. Deliberately its own page rather than a tab inside the Plan modal —
 * see the section header above `SizingScratchpadEntry` in main.py for why
 * the two are kept apart end to end.
 */
export default function SizingPage() {
  return (
    <div className="min-h-screen bg-obsidian-bg">
      <header className="border-b border-obsidian-border bg-obsidian-card/60 backdrop-blur-md">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-4">
          <div className="flex items-center space-x-3">
            <div className="rounded-lg border border-obsidian-border bg-obsidian-bg p-2 text-emerald-400">
              <Calculator className="h-5 w-5" />
            </div>
            <div>
              <h1 className="text-lg font-bold tracking-tight text-slate-100">
                SIZING SCRATCHPAD
              </h1>
              <p className="text-[11px] text-obsidian-muted">
                Record entry, stop, target and shares fast — promote to a real plan when there is time
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

      <main className="mx-auto max-w-5xl px-6 py-6">
        <SizingScratchpad />
      </main>
    </div>
  );
}

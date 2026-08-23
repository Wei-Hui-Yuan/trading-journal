'use client';

import React from 'react';
import { Calculator } from 'lucide-react';

import { PageHeader } from '@/components/PageHeader';
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
      <PageHeader
        icon={Calculator}
        title="SIZING SCRATCHPAD"
        subtitle="Record entry, stop, target and shares fast — promote to a real plan when there is time"
        accent="emerald"
      />

      <main className="mx-auto max-w-5xl px-6 py-6">
        <SizingScratchpad />
      </main>
    </div>
  );
}

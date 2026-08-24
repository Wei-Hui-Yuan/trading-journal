'use client';

import React from 'react';
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
      <main className="mx-auto max-w-5xl px-6 py-6">
        <SizingScratchpad />
      </main>
    </div>
  );
}

'use client';

import React from 'react';
import { Search, X } from 'lucide-react';

export interface HoldingFilters {
  query: string;
  sector: string;
  category: string;
  country: string;
  status: 'open' | 'closed' | 'all';
}

export const DEFAULT_HOLDING_FILTERS: HoldingFilters = {
  query: '',
  sector: '',
  category: '',
  country: '',
  status: 'open',
};

const INPUT =
  'rounded-lg border border-obsidian-border bg-obsidian-bg px-2.5 py-1.5 text-xs ' +
  'text-slate-100 placeholder:text-slate-700 focus:border-slate-600 focus:outline-none';
const LABEL = 'sr-only';

/**
 * Presentational only -- filter state lives in InvestmentTable, this just
 * renders it and reports changes. No data fetching here.
 *
 * Every dropdown's option list is derived from the WHOLE book (open and
 * closed alike), passed in via `options`, not from whatever the other
 * filters currently leave visible -- narrowing options as you filter feels
 * clever and then feels broken, when a sector you were about to pick
 * disappears because Status briefly excluded it.
 */
export const HoldingsFilterBar: React.FC<{
  filters: HoldingFilters;
  onChange: (filters: HoldingFilters) => void;
  options: {
    sectors: string[];
    categories: string[];
    countries: string[];
  };
  openCount: number;
  closedCount: number;
}> = ({ filters, onChange, options, openCount, closedCount }) => {
  const isDefault =
    filters.query === DEFAULT_HOLDING_FILTERS.query &&
    filters.sector === DEFAULT_HOLDING_FILTERS.sector &&
    filters.category === DEFAULT_HOLDING_FILTERS.category &&
    filters.country === DEFAULT_HOLDING_FILTERS.country &&
    filters.status === DEFAULT_HOLDING_FILTERS.status;

  const set = <K extends keyof HoldingFilters>(key: K, value: HoldingFilters[K]) =>
    onChange({ ...filters, [key]: value });

  return (
    <div className="flex flex-wrap items-center gap-2">
      <label className="relative flex-1 min-w-[160px] max-w-xs">
        <span className={LABEL}>Search holdings</span>
        <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-obsidian-muted" />
        <input
          value={filters.query}
          onChange={(e) => set('query', e.target.value)}
          placeholder="Search ticker or name…"
          className={`${INPUT} w-full pl-8`}
        />
      </label>

      <label>
        <span className={LABEL}>Status</span>
        <select
          value={filters.status}
          onChange={(e) => set('status', e.target.value as HoldingFilters['status'])}
          className={INPUT}
        >
          <option value="open">Open ({openCount})</option>
          <option value="closed">Closed ({closedCount})</option>
          <option value="all">All statuses</option>
        </select>
      </label>

      <label>
        <span className={LABEL}>Sector</span>
        <select
          value={filters.sector}
          onChange={(e) => set('sector', e.target.value)}
          className={INPUT}
        >
          <option value="">All sectors</option>
          {options.sectors.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </label>

      <label>
        <span className={LABEL}>Category</span>
        <select
          value={filters.category}
          onChange={(e) => set('category', e.target.value)}
          className={INPUT}
        >
          <option value="">All categories</option>
          {options.categories.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </label>

      <label>
        <span className={LABEL}>Country</span>
        <select
          value={filters.country}
          onChange={(e) => set('country', e.target.value)}
          className={INPUT}
        >
          <option value="">All countries</option>
          {options.countries.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </label>

      {!isDefault && (
        <button
          type="button"
          onClick={() => onChange(DEFAULT_HOLDING_FILTERS)}
          className="inline-flex items-center gap-1 rounded-lg border border-obsidian-border px-2.5 py-1.5 text-xs text-obsidian-muted transition-colors hover:border-slate-600 hover:text-slate-200"
        >
          <X className="h-3.5 w-3.5" />
          Clear
        </button>
      )}
    </div>
  );
};

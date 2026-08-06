'use client';

import React, { useMemo, useState } from 'react';
import {
  AlertCircle,
  CalendarClock,
  Coins,
  DownloadCloud,
  Landmark,
  Loader2,
  Pencil,
  Plus,
  Receipt,
  RefreshCw,
  TrendingUp,
  Wallet,
} from 'lucide-react';

import {
  usePortfolio,
  useRefreshPrices,
  useRefreshValuations,
  useSyncTransactions,
} from '@/hooks/useInvestments';
import type { Holding, Portfolio } from '@/types/investments';
import { ValuationModal } from './ValuationModal';
import { AddInvestmentModal } from './AddInvestmentModal';
import { AddHoldingModal } from './AddHoldingModal';
import { TransactionLedgerModal } from './TransactionLedgerModal';
import { EditHoldingModal } from './EditHoldingModal';
import { AllocationPanel } from './AllocationPanel';
import { DEFAULT_HOLDING_FILTERS, HoldingsFilterBar, type HoldingFilters } from './HoldingsFilterBar';

/**
 * Money, in the listed currency and without pretending to more precision than
 * a price carries. Null renders as an em dash rather than 0 -- an absent price
 * and a zero price are different facts, and only one of them is a number.
 */
function money(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return '—';
  return value.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function signedMoney(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return `${value >= 0 ? '+' : '−'}${Math.abs(value).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/** Trailing zeros on a fractional share count are noise. */
function quantity(value: number): string {
  return Number(value.toFixed(8)).toLocaleString('en-US', {
    maximumFractionDigits: 8,
  });
}

const HEAD =
  'px-2.5 py-2 text-[10px] font-semibold uppercase tracking-wider text-slate-300';
const CELL = 'px-2.5 py-2 align-middle';
const NUM = `${CELL} text-right font-mono tabular-nums`;

/**
 * Discount or premium to the model's average intrinsic value.
 *
 * The sign is the opposite of intuition and so is spelled out rather than left
 * to a colour: the API returns (price / value - 1), so POSITIVE means the
 * market is asking MORE than the model says it is worth. Green is therefore
 * the negative number. A bare signed percentage here would be read backwards
 * by anyone who has not just written the formula.
 */
const DiscountPremium: React.FC<{ holding: Holding }> = ({ holding }) => {
  const valuation = holding.valuation;

  if (!valuation) {
    return <span className="text-obsidian-muted" title="Not valued — a fund has no cash flows of its own">—</span>;
  }
  if (!valuation.available) {
    return (
      <span
        className="text-obsidian-muted"
        title={`Needs ${(valuation.missing ?? []).join(', ')} — set them in the valuation modal`}
      >
        no model
      </span>
    );
  }
  const premium = valuation.premium_pct;
  if (premium === null || premium === undefined) {
    return <span className="text-obsidian-muted" title="No price to compare against">—</span>;
  }

  const discounted = premium < 0;
  return (
    <span
      className={discounted ? 'text-win' : 'text-loss'}
      title={
        discounted
          ? 'Trading below the model’s value — a discount'
          : 'Trading above the model’s value — a premium'
      }
    >
      {discounted ? '−' : '+'}
      {Math.abs(premium).toFixed(1)}%
    </span>
  );
};

const KPI_CARD =
  'rounded-xl border border-obsidian-border bg-obsidian-card p-3';
const KPI_LABEL =
  'flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wider text-obsidian-muted';
const KPI_FIGURE = 'mt-1 font-mono text-xl font-bold';
const KPI_SUBTITLE = 'mt-0.5 text-[10px] text-obsidian-muted';

/**
 * The four numbers the table's footer row already sums, surfaced above it so
 * they don't require scrolling to the bottom of a fourteen-row table to find.
 *
 * Realized P&L and dividends are shown as two distinct figures rather than
 * summed into one -- a closed trade's gain and a dividend received are both
 * "money that came back", but conflating them under one number would hide
 * which of the two actually produced it.
 */
const PortfolioKpiHeader: React.FC<{ portfolio: Portfolio }> = ({ portfolio }) => {
  const {
    holdings,
    total_market_value,
    total_cost_basis,
    total_unrealized_pnl,
    total_realized_pnl,
    total_dividends,
  } = portfolio;

  const openPositions = holdings.filter((h) => h.quantity > 0).length;
  const transactionCount = holdings.reduce((sum, h) => sum + h.transaction_count, 0);

  // total_market_value and total_unrealized_pnl both skip a holding with no
  // current_price entirely (see the backend: "nothing held is not the same as
  // held and worth zero"), but total_cost_basis does not -- it counts every
  // holding regardless of whether it has ever been priced. Dividing the two
  // portfolio totals directly would mix a priced-only numerator with an
  // every-holding denominator, which can even get the SIGN wrong when enough
  // capital sits in unpriced positions. Scoping both sides of the ratio to the
  // same priced subset is what a holding's own unrealized_pnl_pct already
  // does; this is that identity applied to the total instead of one row.
  const pricedHoldings = holdings.filter((h) => h.market_value !== null);
  const pricedCostBasis = pricedHoldings.reduce((sum, h) => sum + h.cost_basis, 0);
  const unrealizedPct =
    pricedCostBasis > 0 ? (total_unrealized_pnl / pricedCostBasis) * 100 : null;

  // Real capital, sitting in a real position, that current_market_value and
  // unrealized P&L above are silently not accounting for -- not because it is
  // worthless, but because no price has ever been fetched for it. Worth
  // saying out loud rather than letting "Capital invested" and "Total
  // portfolio value" quietly disagree by exactly this amount.
  const unpriced = holdings.filter((h) => h.quantity > 0 && h.market_value === null);
  const unpricedCostBasis = unpriced.reduce((sum, h) => sum + h.cost_basis, 0);

  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <div className={KPI_CARD}>
        <div className={KPI_LABEL}>
          <Wallet className="h-3.5 w-3.5 text-sky-400" />
          Total portfolio value
        </div>
        <div className={`${KPI_FIGURE} text-slate-100`}>{money(total_market_value)}</div>
        <div className={KPI_SUBTITLE}>
          {openPositions} open position{openPositions === 1 ? '' : 's'}
        </div>
        {unpriced.length > 0 && (
          <div
            className="mt-0.5 text-[10px] text-amber-400"
            title={`Never priced: ${unpriced.map((h) => h.ticker).join(', ')}`}
          >
            +{unpriced.length} unpriced ({money(unpricedCostBasis)} not counted)
          </div>
        )}
      </div>

      <div className={KPI_CARD}>
        <div className={KPI_LABEL}>
          <Landmark className="h-3.5 w-3.5 text-slate-400" />
          Capital invested
        </div>
        <div className={`${KPI_FIGURE} text-slate-100`}>{money(total_cost_basis)}</div>
        <div className={KPI_SUBTITLE}>
          {transactionCount} transaction{transactionCount === 1 ? '' : 's'}
        </div>
      </div>

      <div className={KPI_CARD}>
        <div className={KPI_LABEL}>
          <TrendingUp className="h-3.5 w-3.5 text-slate-400" />
          Unrealized P&amp;L
        </div>
        <div
          className={`${KPI_FIGURE} ${total_unrealized_pnl >= 0 ? 'text-win' : 'text-loss'}`}
        >
          {signedMoney(total_unrealized_pnl)}
        </div>
        <div className={KPI_SUBTITLE}>
          {unrealizedPct === null ? (
            'vs cost basis'
          ) : (
            <>
              {unrealizedPct >= 0 ? '+' : '−'}
              {Math.abs(unrealizedPct).toFixed(1)}% vs cost basis
            </>
          )}
        </div>
      </div>

      <div className={KPI_CARD}>
        <div className={KPI_LABEL}>
          <Coins className="h-3.5 w-3.5 text-slate-400" />
          Realized P&amp;L
        </div>
        <div
          className={`${KPI_FIGURE} ${total_realized_pnl >= 0 ? 'text-win' : 'text-loss'}`}
        >
          {signedMoney(total_realized_pnl)}
        </div>
        <div className={KPI_SUBTITLE}>{money(total_dividends)} in dividends</div>
      </div>
    </div>
  );
};

const dateFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  dateStyle: 'medium',
  timeStyle: 'short',
});

/**
 * Freshness of the fetched inputs, not of the page load.
 *
 * The toolbar used to print "as of {portfolio.as_of}" -- the time the ROW WAS
 * READ, which is always approximately now and told you nothing about whether
 * the numbers behind it were a month old. This reads `inputs.auto.updated_at`
 * instead, which is when the refresh actually last touched each ticker.
 *
 * Counted against VALUABLE holdings only: an ETF is never fetched and
 * counting it as "missing" would make the coverage figure worse than the
 * refresh actually is.
 */
const ValuationStatusCard: React.FC<{
  holdings: Holding[];
  onRunNow: () => void;
  running: boolean;
}> = ({ holdings, onRunNow, running }) => {
  const valuable = holdings.filter((h) => h.is_valuable);
  const valued = valuable.filter((h) => h.valuation?.available).length;
  // NOT "no auto row at all" -- ASML and Novo Nordisk both HAVE one; Finviz
  // supplied their growth rate, only the FMP/Finnhub fundamentals came back
  // empty (foreign filers, see market_data.py). available === false is the
  // one signal that is actually true regardless of which input is missing.
  const needsInput = valuable.length - valued;

  const timestamps = valuable
    .map((h) => h.inputs.auto?.updated_at)
    .filter((t): t is string => !!t)
    .map((t) => new Date(t).getTime());
  const newest = timestamps.length ? Math.max(...timestamps) : null;
  const oldest = timestamps.length ? Math.min(...timestamps) : null;

  return (
    <div className="flex max-w-xs items-start gap-3 rounded-xl border border-obsidian-border bg-obsidian-card p-3">
      <div className="rounded-lg bg-sky-500/10 p-1.5 text-sky-400">
        <CalendarClock className="h-4 w-4" />
      </div>
      <div className="min-w-0">
        <div className="text-[10px] font-medium uppercase tracking-wider text-obsidian-muted">
          Valuation data
        </div>
        <div className="mt-0.5 font-mono text-lg font-bold text-slate-100">
          {valued}/{valuable.length}
          <span className="ml-1 text-xs font-normal text-obsidian-muted">valued</span>
        </div>
        <div className="mt-0.5 text-[10px] text-obsidian-muted">
          {newest === null
            ? 'never refreshed'
            : newest === oldest
              ? `refreshed ${dateFmt.format(newest)} ET`
              : `refreshed ${dateFmt.format(oldest as number)}–${dateFmt.format(newest)} ET`}
        </div>
        {needsInput > 0 && (
          <div className="mt-0.5 text-[10px] text-amber-400">
            {needsInput} need{needsInput === 1 ? 's' : ''} manual input
          </div>
        )}

        {/* Deliberately a text link, not a button matching the toolbar --
            the scheduled job is the primary path now, and this is the
            escape hatch for what it cannot yet cover, not an alternative to
            it. Loud styling here would put the two on equal footing. */}
        <button
          type="button"
          disabled={running}
          onClick={onRunNow}
          title="Re-fetches fundamentals and growth for the whole book right now, ahead of the scheduled run. Takes about ninety seconds."
          className="mt-1.5 text-[10px] text-slate-500 underline decoration-dotted transition-colors hover:text-slate-300 disabled:opacity-50"
        >
          {running ? 'Refreshing (~90s)…' : 'Run now'}
        </button>
      </div>
    </div>
  );
};

/** Weight as a number and a bar, because relative size is the actual question. */
const Weight: React.FC<{ pct: number | null }> = ({ pct }) => {
  if (pct === null) return <span className="text-obsidian-muted">—</span>;
  return (
    <div className="flex items-center justify-end gap-2">
      <div className="hidden h-1 w-10 overflow-hidden rounded-full bg-obsidian-border sm:block">
        <div
          className="h-full rounded-full bg-slate-500"
          style={{ width: `${Math.min(100, pct)}%` }}
        />
      </div>
      <span className="w-12 text-right">{pct.toFixed(1)}%</span>
    </div>
  );
};

export const InvestmentTable: React.FC = () => {
  const { data: portfolio, isLoading, error } = usePortfolio();
  const refreshPrices = useRefreshPrices();
  const refreshValuations = useRefreshValuations();
  const syncTransactions = useSyncTransactions();

  const [selected, setSelected] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [addingStock, setAddingStock] = useState(false);
  const [ledgerTicker, setLedgerTicker] = useState<string | null>(null);
  const [editingTicker, setEditingTicker] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // Scoped to the table body only -- AllocationPanel and the KPI header keep
  // reading the full `holdings` array regardless of what's filtered here, the
  // same way `showClosed` used to work before it folded into this. Session
  // state, not persisted: resets to the default (open positions only) on
  // reload rather than remembering a filter from last time.
  const [filters, setFilters] = useState<HoldingFilters>(DEFAULT_HOLDING_FILTERS);

  // Largest position first, which is how a portfolio is actually read -- the
  // question is nearly always "what am I most exposed to". Holdings with no
  // position sort to the bottom alphabetically rather than being interleaved
  // among the real ones.
  const holdings = useMemo(() => {
    const rows = [...(portfolio?.holdings ?? [])];
    rows.sort((a, b) => {
      const left = a.market_value ?? -1;
      const right = b.market_value ?? -1;
      if (left !== right) return right - left;
      return a.ticker.localeCompare(b.ticker);
    });
    return rows;
  }, [portfolio]);
  const selectedHolding = holdings.find((h) => h.ticker === selected) ?? null;
  const editingHolding = holdings.find((h) => h.ticker === editingTicker) ?? null;

  // What the Sector/Type/Country/Currency comboboxes -- and now the filter
  // bar's Sector/Category/Country dropdowns -- offer: distinct values already
  // used anywhere in the book, closed positions included. A fully-exited
  // holding's classification is still real data worth reoffering, and these
  // lists must stay derived from the WHOLE book regardless of what the
  // filter bar currently narrows the table to (see HoldingsFilterBar).
  const fieldOptions = useMemo(() => {
    const distinct = (values: (string | null)[]) =>
      Array.from(new Set(values.filter((v): v is string => !!v && v.trim() !== '')))
        .sort((a, b) => a.localeCompare(b));
    return {
      sectors: distinct(holdings.map((h) => h.sector)),
      categories: distinct(holdings.map((h) => h.category)),
      types: distinct(holdings.map((h) => h.holding_type)),
      countries: distinct(holdings.map((h) => h.country)),
      currencies: distinct(holdings.map((h) => h.listed_currency)),
    };
  }, [holdings]);

  const openCount = holdings.filter((h) => h.quantity > 0).length;
  const closedCount = holdings.filter((h) => h.quantity === 0).length;

  // The one place all five filters apply, in order: status narrows to
  // open/closed/all first, the three classification dropdowns each skip
  // themselves when unset, and the search text matches last against
  // whatever survived. Everywhere else on the page (AllocationPanel, the KPI
  // header, ValuationStatusCard) keeps reading `holdings` directly.
  const visibleHoldings = useMemo(() => {
    const query = filters.query.trim().toLowerCase();
    return holdings.filter((h) => {
      if (filters.status === 'open' && h.quantity <= 0) return false;
      if (filters.status === 'closed' && h.quantity !== 0) return false;
      if (filters.sector && h.sector !== filters.sector) return false;
      if (filters.category && h.category !== filters.category) return false;
      if (filters.country && h.country !== filters.country) return false;
      if (query) {
        const haystack = `${h.ticker} ${h.name ?? ''}`.toLowerCase();
        if (!haystack.includes(query)) return false;
      }
      return true;
    });
  }, [holdings, filters]);

  const isFiltered =
    filters.query !== DEFAULT_HOLDING_FILTERS.query ||
    filters.sector !== DEFAULT_HOLDING_FILTERS.sector ||
    filters.category !== DEFAULT_HOLDING_FILTERS.category ||
    filters.country !== DEFAULT_HOLDING_FILTERS.country ||
    filters.status !== DEFAULT_HOLDING_FILTERS.status;

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 py-16 text-sm text-obsidian-muted">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading the book…
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-start gap-2 rounded-lg border border-loss/30 bg-loss/5 p-4 text-sm text-loss">
        <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
        <span>{error.message}</span>
      </div>
    );
  }

  const totals = portfolio;

  return (
    <div className="space-y-4">
      {totals && <PortfolioKpiHeader portfolio={totals} />}

      {holdings.length > 0 && (
        <AllocationPanel holdings={holdings} onSetTarget={setEditingTicker} />
      )}

      {/* ---------------- toolbar ---------------- */}
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => setAdding(true)}
          className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-xs font-medium text-emerald-300 transition-colors hover:bg-emerald-500/20"
        >
          <Plus className="h-3.5 w-3.5" />
          Record transaction
        </button>

        {/* Separate from "Record transaction" -- that path creates a holding
            implicitly alongside its first fill. This is for the opposite
            order: classify and value a ticker before owning any of it. */}
        <button
          type="button"
          onClick={() => setAddingStock(true)}
          className="inline-flex items-center gap-1.5 rounded-lg border border-obsidian-border bg-obsidian-bg px-3 py-2 text-xs text-obsidian-muted transition-colors hover:border-slate-600 hover:text-slate-200"
        >
          <Plus className="h-3.5 w-3.5" />
          Add stock
        </button>

        <button
          type="button"
          disabled={refreshPrices.isPending}
          onClick={() => {
            setNotice(null);
            refreshPrices.mutate(undefined, {
              onSuccess: (r) => {
                const head = `Prices: ${r.updated} updated${
                  r.failed ? `, ${r.failed} failed` : ''
                }.`;
                // The endpoint reports WHY each ticker failed; printing only
                // the count turned a configuration error ("FMP_KEY is not
                // set" on the server, 20 times over) into what looked like a
                // dead button. Distinct reasons only -- one bad key produces
                // the same sentence per holding, and twenty copies of it is
                // not twenty pieces of information.
                const reasons = Array.from(
                  new Set(r.failures.map((f) => f.detail))
                );
                setNotice(reasons.length ? `${head} ${reasons.join(' ')}` : head);
              },
              onError: (e) => setNotice(e.message),
            });
          }}
          className="inline-flex items-center gap-1.5 rounded-lg border border-obsidian-border bg-obsidian-bg px-3 py-2 text-xs text-obsidian-muted transition-colors hover:border-slate-600 hover:text-slate-200 disabled:opacity-50"
        >
          {refreshPrices.isPending ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <RefreshCw className="h-3.5 w-3.5" />
          )}
          Refresh prices
        </button>

        {/* Pulls from the account's OWN Flex query -- a separate pipeline
            from the trading journal's broker sync, and can take a while for
            the same reason that one does: IBKR compiles the report on
            request rather than serving one on hand. */}
        <button
          type="button"
          disabled={syncTransactions.isPending}
          onClick={() => {
            setNotice(null);
            syncTransactions.mutate(undefined, {
              onSuccess: (r) => {
                const clauses = [`${r.imported} imported`];
                if (r.duplicates > 0) {
                  clauses.push(`${r.duplicates} duplicate${r.duplicates === 1 ? '' : 's'}`);
                }
                if (r.holdings_created.length > 0) {
                  clauses.push(
                    `${r.holdings_created.length} new holding${
                      r.holdings_created.length === 1 ? '' : 's'
                    } (${r.holdings_created.join(', ')})`
                  );
                }
                if (r.skipped > 0) {
                  clauses.push(`${r.skipped} skipped`);
                }
                if (r.queries_failed.length > 0) {
                  clauses.push(
                    `${r.queries_failed.length} quer${
                      r.queries_failed.length === 1 ? 'y' : 'ies'
                    } unavailable`
                  );
                }
                setNotice(`Sync IBKR: ${clauses.join(', ')}.`);
              },
              onError: (e) => setNotice(e.message),
            });
          }}
          className="inline-flex items-center gap-1.5 rounded-lg border border-obsidian-border bg-obsidian-bg px-3 py-2 text-xs text-obsidian-muted transition-colors hover:border-slate-600 hover:text-slate-200 disabled:opacity-50"
        >
          {syncTransactions.isPending ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <DownloadCloud className="h-3.5 w-3.5" />
          )}
          Sync IBKR
        </button>

      </div>

      {/* No "Refresh valuations" button here on purpose -- a Northflank Cron
          Job runs this monthly against /api/investments/refresh now (see
          verify_clerk_or_cron_token in auth.py). The status card is the
          record of that; the text link inside it is the deliberately quiet
          escape hatch for the case the schedule cannot cover -- a ticker
          added five minutes ago, valued next month otherwise. */}
      {portfolio && (
        <ValuationStatusCard
          holdings={holdings}
          onRunNow={() => {
            setNotice(null);
            refreshValuations.mutate(false, {
              onSuccess: (r) =>
                setNotice(
                  `Valuations: ${r.refreshed} refreshed, ${r.skipped} still fresh` +
                    `${r.failed ? `, ${r.failed} failed` : ''}.`
                ),
              onError: (e) => setNotice(e.message),
            });
          }}
          running={refreshValuations.isPending}
        />
      )}

      {notice && (
        <div className="rounded-lg border border-obsidian-border bg-obsidian-card px-3 py-2 text-xs text-slate-300">
          {notice}
        </div>
      )}

      {/* ---------------- the table ---------------- */}
      {holdings.length > 0 && (
        <HoldingsFilterBar
          filters={filters}
          onChange={setFilters}
          options={fieldOptions}
          openCount={openCount}
          closedCount={closedCount}
        />
      )}

      {holdings.length === 0 ? (
        <div className="rounded-xl border border-dashed border-obsidian-border px-6 py-16 text-center">
          <p className="text-sm text-slate-300">Nothing in the book yet.</p>
          <p className="mt-1 text-xs text-obsidian-muted">
            Record a transaction and the holding is created with it.
          </p>
        </div>
      ) : visibleHoldings.length === 0 && isFiltered ? (
        <div className="rounded-xl border border-dashed border-obsidian-border px-6 py-16 text-center">
          <p className="text-sm text-slate-300">No holdings match these filters.</p>
          <p className="mt-1 text-xs text-obsidian-muted">
            <button
              type="button"
              onClick={() => setFilters(DEFAULT_HOLDING_FILTERS)}
              className="text-slate-300 underline decoration-dotted hover:text-slate-100"
            >
              Clear filters
            </button>
            .
          </p>
        </div>
      ) : visibleHoldings.length === 0 ? (
        <div className="rounded-xl border border-dashed border-obsidian-border px-6 py-16 text-center">
          <p className="text-sm text-slate-300">No active positions.</p>
          <p className="mt-1 text-xs text-obsidian-muted">
            Every holding here has been fully exited.{' '}
            <button
              type="button"
              onClick={() => setFilters((f) => ({ ...f, status: 'all' }))}
              className="text-slate-300 underline decoration-dotted hover:text-slate-100"
            >
              Show closed positions
            </button>
            .
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-obsidian-border">
          <table className="w-full min-w-[1100px] border-collapse text-xs">
            <thead>
              {/* One band across the top, as on the sheet this replaces. */}
              <tr className="border-b border-obsidian-border bg-slate-700/40">
                <th className={`${HEAD} text-left`}>Ticker</th>
                <th className={`${HEAD} text-left`}>Sector</th>
                <th className={`${HEAD} text-center`}>Country</th>
                <th className={`${HEAD} text-right`}>Qty</th>
                <th className={`${HEAD} text-right`}>Avg cost</th>
                <th className={`${HEAD} text-right`}>Price</th>
                <th className={`${HEAD} text-right`}>Market value</th>
                <th className={`${HEAD} text-right`}>Unrealised P&L</th>
                <th className={`${HEAD} text-right`}>Intrinsic value</th>
                <th className={`${HEAD} text-right`}>Disc / Prem</th>
                <th className={`${HEAD} text-right`}>Weight</th>
              </tr>
            </thead>

            <tbody>
              {visibleHoldings.map((holding, index) => {
                const pnl = holding.unrealized_pnl;
                return (
                  <tr
                    key={holding.ticker}
                    onClick={() => setSelected(holding.ticker)}
                    className={`cursor-pointer border-b border-obsidian-border/60 transition-colors hover:bg-slate-700/25 ${
                      index % 2 === 1 ? 'bg-obsidian-card/40' : ''
                    }`}
                    title="Open the valuation inputs"
                  >
                    <td className={CELL}>
                      <div className="flex items-center gap-1.5">
                        <span className="font-semibold text-slate-100">{holding.ticker}</span>
                        {/* stopPropagation -- the row's own onClick opens the
                            valuation modal, and this needs to open a
                            different one instead of both firing. */}
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            setLedgerTicker(holding.ticker);
                          }}
                          title={`${holding.transaction_count} transaction(s) — view, edit or delete`}
                          className="text-obsidian-muted transition-colors hover:text-slate-200"
                        >
                          <Receipt className="h-3 w-3" />
                        </button>
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            setEditingTicker(holding.ticker);
                          }}
                          title="Edit classification, or delete this holding"
                          className="text-obsidian-muted transition-colors hover:text-slate-200"
                        >
                          <Pencil className="h-3 w-3" />
                        </button>
                      </div>
                      {holding.name && (
                        <div className="max-w-[150px] truncate text-[10px] text-obsidian-muted">
                          {holding.name}
                        </div>
                      )}
                    </td>

                    <td className={`${CELL} text-slate-300`}>
                      <div className="max-w-[130px] truncate">{holding.sector ?? '—'}</div>
                      {holding.category && (
                        <span className="text-[10px] text-obsidian-muted">
                          {holding.category}
                        </span>
                      )}
                    </td>

                    <td className={`${CELL} text-center text-slate-300`}>
                      <div>{holding.country ?? '—'}</div>
                      <div className="text-[10px] text-obsidian-muted">
                        {holding.listed_currency}
                      </div>
                    </td>

                    <td className={`${NUM} text-slate-300`}>{quantity(holding.quantity)}</td>
                    <td className={`${NUM} text-slate-300`}>{money(holding.average_cost)}</td>
                    <td className={`${NUM} text-slate-100`}>{money(holding.current_price)}</td>
                    <td className={`${NUM} text-slate-100`}>{money(holding.market_value)}</td>

                    <td className={NUM}>
                      <span
                        className={
                          pnl === null
                            ? 'text-obsidian-muted'
                            : pnl >= 0
                              ? 'text-win'
                              : 'text-loss'
                        }
                      >
                        {signedMoney(pnl)}
                      </span>
                      {holding.unrealized_pnl_pct !== null && (
                        <div className="text-[10px] text-obsidian-muted">
                          {holding.unrealized_pnl_pct >= 0 ? '+' : '−'}
                          {Math.abs(holding.unrealized_pnl_pct).toFixed(1)}%
                        </div>
                      )}
                    </td>

                    <td className={`${NUM} text-slate-300`}>
                      {holding.valuation?.available
                        ? money(holding.valuation.average_intrinsic_value)
                        : '—'}
                      {holding.valuation?.overridden_fields?.length ? (
                        <div
                          className="text-[10px] text-amber-400"
                          title={`Your override: ${holding.valuation.overridden_fields.join(', ')}`}
                        >
                          edited
                        </div>
                      ) : null}
                    </td>

                    <td className={NUM}>
                      <DiscountPremium holding={holding} />
                    </td>

                    <td className={NUM}>
                      <Weight pct={holding.portfolio_weight_pct} />
                    </td>
                  </tr>
                );
              })}
            </tbody>

            {totals && (
              <tfoot>
                <tr className="border-t-2 border-obsidian-border bg-slate-700/30 font-semibold">
                  <td className={`${CELL} text-slate-200`} colSpan={6}>
                    Total
                  </td>
                  <td className={`${NUM} text-slate-100`}>
                    {money(totals.total_market_value)}
                  </td>
                  <td className={NUM}>
                    <span
                      className={
                        totals.total_unrealized_pnl >= 0 ? 'text-win' : 'text-loss'
                      }
                    >
                      {signedMoney(totals.total_unrealized_pnl)}
                    </span>
                  </td>
                  {/* Intrinsic values are per share and belong to different
                      companies; a column sum would be arithmetic on unrelated
                      units rather than a portfolio figure. */}
                  <td className={`${NUM} text-obsidian-muted`}>—</td>
                  <td className={`${NUM} text-obsidian-muted`}>—</td>
                  <td className={`${NUM} text-slate-300`}>
                    {totals.total_market_value > 0 ? '100.0%' : '—'}
                  </td>
                </tr>
              </tfoot>
            )}
          </table>
        </div>
      )}

      {/* Realised P&L and dividends are real money that the unrealised column
          cannot show, and a book held for years accumulates a lot of both. */}
      {totals && (totals.total_realized_pnl !== 0 || totals.total_dividends !== 0) && (
        <div className="flex flex-wrap gap-x-6 gap-y-1 px-1 font-mono text-[11px] text-obsidian-muted">
          <span>
            Cost basis <span className="text-slate-300">{money(totals.total_cost_basis)}</span>
          </span>
          <span>
            Realised{' '}
            <span className={totals.total_realized_pnl >= 0 ? 'text-win' : 'text-loss'}>
              {signedMoney(totals.total_realized_pnl)}
            </span>
          </span>
          <span>
            Dividends <span className="text-win">{money(totals.total_dividends)}</span>
          </span>
        </div>
      )}

      {selectedHolding && (
        <ValuationModal
          holding={selectedHolding}
          onClose={() => setSelected(null)}
        />
      )}

      <AddInvestmentModal open={adding} onClose={() => setAdding(false)} />
      <AddHoldingModal
        open={addingStock}
        onClose={() => setAddingStock(false)}
        sectorOptions={fieldOptions.sectors}
        typeOptions={fieldOptions.types}
        countryOptions={fieldOptions.countries}
        currencyOptions={fieldOptions.currencies}
        totalCostBasis={totals?.total_cost_basis ?? 0}
      />
      {ledgerTicker && (
        <TransactionLedgerModal
          ticker={ledgerTicker}
          onClose={() => setLedgerTicker(null)}
        />
      )}
      {editingHolding && (
        <EditHoldingModal
          holding={editingHolding}
          onClose={() => setEditingTicker(null)}
          sectorOptions={fieldOptions.sectors}
          typeOptions={fieldOptions.types}
          countryOptions={fieldOptions.countries}
          currencyOptions={fieldOptions.currencies}
          totalCostBasis={totals?.total_cost_basis ?? 0}
        />
      )}
    </div>
  );
};

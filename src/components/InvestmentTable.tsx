'use client';

import React, { useMemo, useState } from 'react';
import {
  AlertCircle,
  CalendarClock,
  Loader2,
  Plus,
  Receipt,
  RefreshCw,
} from 'lucide-react';

import {
  usePortfolio,
  useRefreshPrices,
  useRefreshValuations,
} from '@/hooks/useInvestments';
import type { Holding } from '@/types/investments';
import { ValuationModal } from './ValuationModal';
import { AddInvestmentModal } from './AddInvestmentModal';
import { AddHoldingModal } from './AddHoldingModal';
import { TransactionLedgerModal } from './TransactionLedgerModal';

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

  const [selected, setSelected] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [addingStock, setAddingStock] = useState(false);
  const [ledgerTicker, setLedgerTicker] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

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
              onSuccess: (r) =>
                setNotice(
                  `Prices: ${r.updated} updated${r.failed ? `, ${r.failed} failed` : ''}.`
                ),
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
      {holdings.length === 0 ? (
        <div className="rounded-xl border border-dashed border-obsidian-border px-6 py-16 text-center">
          <p className="text-sm text-slate-300">Nothing in the book yet.</p>
          <p className="mt-1 text-xs text-obsidian-muted">
            Record a transaction and the holding is created with it.
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
              {holdings.map((holding, index) => {
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
      <AddHoldingModal open={addingStock} onClose={() => setAddingStock(false)} />
      {ledgerTicker && (
        <TransactionLedgerModal
          ticker={ledgerTicker}
          onClose={() => setLedgerTicker(null)}
        />
      )}
    </div>
  );
};

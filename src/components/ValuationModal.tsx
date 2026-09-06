'use client';

import React, { useMemo, useState } from 'react';
import { AlertCircle, Check, Loader2, RotateCcw, X } from 'lucide-react';

import {
  useClearValuationOverride,
  useSetValuationOverride,
  useUpdateHolding,
} from '@/hooks/useInvestments';
import type {
  Holding,
  ValuationMethod,
  ValuationOverridePayload,
} from '@/types/investments';

/**
 * The three ways to run the same twenty-year model.
 *
 * One engine, one discount rate, one growth schedule, one debt-and-cash
 * bridge — the ONLY thing that differs between these numbers is which line
 * of the financial statements gets grown for twenty years. That is what
 * makes them worth showing together rather than as three separate opinions:
 *
 *     DCF-20 far above DFCF-20   the operating cash is going into capex
 *     DNI-20 far above both      accounting earnings the cash flow
 *                                statement does not corroborate
 *
 * Short names are the reference tool's own, so a figure here and a bar on
 * its chart can be checked against each other by name.
 */
const METHODS: { key: ValuationMethod; short: string; label: string }[] = [
  { key: 'free_cash_flow', short: 'DFCF-20', label: 'Free cash flow' },
  { key: 'operating_cash_flow', short: 'DCF-20', label: 'Operating cash flow' },
  { key: 'net_income', short: 'DNI-20', label: 'Net income' },
];

/**
 * The inputs the DCF takes, in the order the workbook lists them.
 *
 * `percent` fields are stored as decimal fractions (0.1269) and shown as
 * percentages, because that is how they are read and quoted. Converting at
 * this boundary keeps the engine working in one unit -- a growth rate that is
 * 12.69 in one place and 0.1269 in another is a hundredfold error waiting for
 * whichever field gets copied.
 */
const FIELDS: {
  key: keyof ValuationOverridePayload;
  label: string;
  hint?: string;
  percent?: boolean;
}[] = [
  { key: 'base_flow', label: 'Free cash flow', hint: 'millions — feeds DFCF-20' },
  // The other two flows, editable for the same reason base_flow is: no
  // provider covers every holding, and a company nobody reports on can
  // still be valued by hand. Left blank they simply produce no bar.
  { key: 'operating_cash_flow', label: 'Operating cash flow',
    hint: 'millions — feeds DCF-20' },
  { key: 'net_income', label: 'Net income', hint: 'millions — feeds DNI-20' },
  { key: 'shares_outstanding', label: 'Shares outstanding', hint: 'millions' },
  { key: 'total_debt', label: 'Total debt', hint: 'millions, excl. leases' },
  { key: 'cash_and_st', label: 'Cash + short-term', hint: 'millions' },
  { key: 'beta', label: 'Beta', hint: 'bucketed onto the risk table' },
  { key: 'growth_1_5', label: 'Growth, years 1–5', percent: true,
    hint: 'years 6–10 and 11–20 follow from this' },
  { key: 'discount_rate', label: 'Discount rate', percent: true,
    hint: 'leave blank to derive from beta' },
  { key: 'statement_exchange_rate', label: 'Statement FX rate',
    hint: '1 USD in the filing currency -- only needed when it differs from USD' },
];

function display(value: number | null | undefined, percent?: boolean): string {
  if (value === null || value === undefined) return '—';
  if (percent) return `${(value * 100).toFixed(2)}%`;
  return value.toLocaleString('en-US', { maximumFractionDigits: 2 });
}

/**
 * A premium over intrinsic value, signed.
 *
 * POSITIVE means the market is asking more than the model says it is worth,
 * so the negative figure is the interesting one. Null renders as an em dash
 * rather than 0%, which would read as "fairly priced" — a claim, where the
 * truth is that there was nothing to compare against.
 */
function premium(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return `${value < 0 ? '−' : '+'}${Math.abs(value).toFixed(1)}%`;
}

/** Empty string means "no override", which is different from zero. */
function parse(raw: string, percent?: boolean): number | null {
  const text = raw.trim();
  if (!text) return null;
  const parsed = Number(text);
  if (Number.isNaN(parsed)) return null;
  return percent ? parsed / 100 : parsed;
}

export const ValuationModal: React.FC<{
  holding: Holding;
  onClose: () => void;
}> = ({ holding, onClose }) => {
  const save = useSetValuationOverride();
  const clear = useClearValuationOverride();
  // The method is a property of the holding, not of the override — it picks
  // between results rather than feeding one — so it rides the ordinary
  // holding patch rather than earning an endpoint of its own.
  const switchMethod = useUpdateHolding();
  const [error, setError] = useState<string | null>(null);

  const auto = holding.inputs.auto;
  const override = holding.inputs.override;
  const valuation = holding.valuation;

  // Seeded from the existing override only. Pre-filling from `auto` would turn
  // every fetched value into a user-set one the moment the modal was opened,
  // and the next refresh would then be unable to update anything.
  const [draft, setDraft] = useState<Record<string, string>>(() => {
    const seed: Record<string, string> = {};
    for (const field of FIELDS) {
      const value = override?.[field.key as keyof typeof override] as
        | number
        | null
        | undefined;
      seed[field.key] =
        value === null || value === undefined
          ? ''
          : String(field.percent ? value * 100 : value);
    }
    return seed;
  });

  const overridden = useMemo(
    () => new Set(valuation?.overridden_fields ?? []),
    [valuation]
  );

  // `valuation.method` is what the server actually valued on; the holding's
  // own column is the fallback for a payload that predates the choice. They
  // agree except when the stored method is unrecognised, and then the
  // server's answer is the honest one — it says which model produced the
  // numbers on screen, not which one was asked for.
  const models = valuation?.models ?? {};
  const method = valuation?.method ?? holding.valuation_method;

  const choose = (next: ValuationMethod) => {
    if (next === method) return;
    setError(null);
    switchMethod.mutate(
      { ticker: holding.ticker, payload: { valuation_method: next } },
      { onError: (e) => setError(e.message) }
    );
  };

  const submit = () => {
    setError(null);
    const payload: ValuationOverridePayload = {};
    for (const field of FIELDS) {
      payload[field.key] = parse(draft[field.key] ?? '', field.percent) as never;
    }
    payload.region = (auto?.region ?? 'US') as never;
    save.mutate(
      { ticker: holding.ticker, payload },
      { onSuccess: onClose, onError: (e) => setError(e.message) }
    );
  };

  return (
    <div
      className="fixed inset-0 z-[60] flex items-start justify-center overflow-y-auto bg-black/70 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="my-8 w-full max-w-3xl rounded-xl border border-obsidian-border bg-obsidian-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* ---------------- header ---------------- */}
        <div className="flex items-start justify-between border-b border-obsidian-border px-5 py-4">
          <div>
            <h2 className="text-base font-bold tracking-tight text-slate-100">
              {holding.ticker}
              {holding.name && (
                <span className="ml-2 text-xs font-normal text-obsidian-muted">
                  {holding.name}
                </span>
              )}
            </h2>
            <p className="mt-0.5 text-[11px] text-obsidian-muted">
              Twenty-year discounted cash flow — three base flows, with and
              without a terminal value
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded p-1 text-obsidian-muted transition-colors hover:text-slate-200"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* ---------------- one engine, three base flows ----------------

            All three are shown at once rather than only the chosen one,
            because the COMPARISON is the output. The three differ in
            exactly one input — which line of the statements is grown — so
            the gap between them is attributable, and a DCF-20 towering over
            its DFCF-20 is a fact about capital expenditure rather than a
            disagreement between models.

            Every method stays clickable even with no figure behind it. That
            is the path into hand-keying one: switch to DNI-20, the banner
            below names `net_income` as what is missing, and the field for it
            is on the right. Blocking the click would make the flow a dead
            end for exactly the holdings no provider covers. */}
        <div className="border-b border-obsidian-border px-5 py-3">
          <div className="mb-2 flex flex-wrap items-baseline gap-x-2">
            <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
              Valuation method
            </span>
            <span className="text-[10px] text-obsidian-muted">
              same twenty years, same discount rate — a different line of the
              statements
            </span>
          </div>
          <div className="grid grid-cols-1 gap-px overflow-hidden rounded-lg border border-obsidian-border bg-obsidian-border sm:grid-cols-3">
            {METHODS.map(({ key, short, label }) => {
              const model = models[key];
              const active = key === method;
              return (
                <button
                  key={key}
                  type="button"
                  onClick={() => choose(key)}
                  disabled={switchMethod.isPending}
                  aria-pressed={active}
                  aria-label={`${short}, ${label}`}
                  className={`px-3 py-2 text-left transition-colors disabled:cursor-wait ${
                    active
                      ? 'bg-obsidian-hover'
                      : 'bg-obsidian-card hover:bg-obsidian-hover/60'
                  }`}
                >
                  <div className="flex items-center gap-1">
                    <span
                      className={`text-[10px] font-semibold uppercase tracking-wide ${
                        active ? 'text-slate-100' : 'text-obsidian-muted'
                      }`}
                    >
                      {short}
                    </span>
                    {active && (
                      <Check className="h-3 w-3 shrink-0 text-emerald-400" />
                    )}
                  </div>
                  <div className="text-[10px] text-obsidian-muted">{label}</div>
                  {model ? (
                    <div className="mt-1 flex items-baseline gap-2 font-mono text-[11px]">
                      <span className="text-slate-100">
                        {display(model.average_intrinsic_value)}
                      </span>
                      <span
                        className={
                          (model.premium_pct ?? 0) < 0 ? 'text-win' : 'text-loss'
                        }
                      >
                        {premium(model.premium_pct)}
                      </span>
                    </div>
                  ) : (
                    <div className="mt-1 font-mono text-[11px] text-obsidian-muted">
                      — no figure
                    </div>
                  )}
                </button>
              );
            })}
          </div>
        </div>

        {/* ---------------- what it produced ---------------- */}
        {valuation?.available ? (
          <div className="grid grid-cols-2 gap-px border-b border-obsidian-border bg-obsidian-border sm:grid-cols-4">
            {[
              ['Base', valuation.base?.intrinsic_value],
              ['Conservative', valuation.conservative?.intrinsic_value],
              ['Average', valuation.average_intrinsic_value],
            ].map(([label, value]) => (
              <div key={String(label)} className="bg-obsidian-card px-4 py-3">
                <div className="text-[10px] uppercase tracking-wide text-obsidian-muted">
                  {label}
                </div>
                <div className="font-mono text-sm text-slate-100">
                  {display(value as number)}
                </div>
              </div>
            ))}
            <div className="bg-obsidian-card px-4 py-3">
              <div className="text-[10px] uppercase tracking-wide text-obsidian-muted">
                vs price {display(holding.current_price)}
              </div>
              <div
                className={`font-mono text-sm ${
                  (valuation.premium_pct ?? 0) < 0 ? 'text-win' : 'text-loss'
                }`}
              >
                {premium(valuation.premium_pct)}
              </div>
            </div>
          </div>
        ) : (
          <div className="flex items-start gap-2 border-b border-obsidian-border bg-amber-500/5 px-5 py-3 text-[11px] text-amber-300">
            <AlertCircle className="mt-px h-3.5 w-3.5 shrink-0" />
            {/* A flow that exists and is not positive is a different problem
                from a flow that is absent, and telling the trader to "fill
                in net_income" when net income is a real reported loss would
                be asking them to correct a fact. */}
            {valuation?.non_positive_flow ? (
              <span>
                Not valued on this method —{' '}
                <span className="font-mono">{valuation.non_positive_flow}</span>{' '}
                is zero or negative, and a flow that shrinks toward nothing
                cannot be grown for twenty years. That is a reading about the
                year, not a gap: pick another method above, or override the
                figure if it is wrong.
              </span>
            ) : (
              <span>
                Not valued yet — needs{' '}
                <span className="font-mono">
                  {(valuation?.missing ?? ['inputs']).join(', ')}
                </span>
                . Fill them in on the right and it will value on save.
              </span>
            )}
          </div>
        )}

        {/* ---------------- the same trade, with a perpetuity ----------------

            Shown BESIDE the twenty-year figures rather than replacing them.
            The two answer different questions -- "what are two decades of
            this flow worth" and "what is it worth if the business simply
            continues" -- and the gap between them is the output. A terminal
            value that doubles the number is saying the thesis rests on year
            21 onwards, which is worth seeing rather than averaging away.

            When the model declines, the reason is stated rather than the row
            hidden. A discount rate at or below perpetual growth has no finite
            answer at all, and that is usually a hand-set rate doing it. */}
        {valuation?.available && valuation.base && (
          <div className="border-b border-obsidian-border px-5 py-3">
            {valuation.base.intrinsic_value_with_terminal != null ? (
              <>
                <div className="mb-1.5 flex flex-wrap items-baseline gap-x-2">
                  <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
                    With terminal value
                  </span>
                  <span className="text-[10px] text-obsidian-muted">
                    perpetuity at{' '}
                    {((valuation.base.perpetual_growth ?? 0) * 100).toFixed(1)}%
                    {valuation.base.terminal_share_pct != null && (
                      <>
                        {' · '}
                        <span
                          className={
                            valuation.base.terminal_share_pct >= 75
                              ? 'text-amber-300'
                              : ''
                          }
                        >
                          {valuation.base.terminal_share_pct.toFixed(0)}% of the
                          value is the perpetuity
                        </span>
                      </>
                    )}
                  </span>
                </div>
                <div className="flex flex-wrap gap-x-6 gap-y-1 font-mono text-[11px]">
                  {[
                    ['Base', valuation.base.intrinsic_value_with_terminal],
                    [
                      'Conservative',
                      valuation.conservative?.intrinsic_value_with_terminal,
                    ],
                    ['Average', valuation.average_intrinsic_value_with_terminal],
                  ].map(([label, value]) => (
                    <span key={String(label)}>
                      <span className="text-obsidian-muted">{label} </span>
                      <span className="text-slate-100">
                        {value == null ? '—' : display(value as number)}
                      </span>
                    </span>
                  ))}
                </div>
                {/* A perpetuity worth three quarters of the answer is a
                    statement about the discount rate, not the business. */}
                {(valuation.base.terminal_share_pct ?? 0) >= 75 && (
                  <p className="mt-1.5 text-[10px] text-amber-300">
                    Most of this comes from after year 20. Treat it as a
                    statement about the discount rate rather than about the
                    business.
                  </p>
                )}
              </>
            ) : (
              <p className="text-[10px] leading-relaxed text-obsidian-muted">
                <span className="uppercase tracking-wide">
                  No terminal value
                </span>{' '}
                — a perpetuity needs the discount rate to sit clearly above
                perpetual growth
                {valuation.base.perpetual_growth != null &&
                  ` (${(valuation.base.perpetual_growth * 100).toFixed(1)}%)`}
                , and this one is{' '}
                {valuation.discount_rate != null
                  ? `${(valuation.discount_rate * 100).toFixed(2)}%`
                  : 'lower'}
                . Below that the formula has no finite answer, so nothing is
                reported rather than a very large number.
              </p>
            )}
          </div>
        )}

        {/* The three stages, which are where the growth rule becomes visible. */}
        {valuation?.available && valuation.base && (
          <div className="border-b border-obsidian-border px-5 py-3">
            <div className="mb-1.5 text-[10px] uppercase tracking-wide text-obsidian-muted">
              Growth applied
            </div>
            <div className="flex flex-wrap gap-x-6 gap-y-1 font-mono text-[11px]">
              <span className="text-slate-300">
                Years 1–5{' '}
                <span className="text-slate-100">
                  {(valuation.base.growth_1_5 * 100).toFixed(2)}%
                </span>
              </span>
              <span className="text-slate-300">
                6–10{' '}
                <span className="text-slate-100">
                  {(valuation.base.growth_6_10 * 100).toFixed(2)}%
                </span>
                <span className="ml-1 text-obsidian-muted">(capped at 15%)</span>
              </span>
              <span className="text-slate-300">
                11–20{' '}
                <span className="text-slate-100">
                  {(valuation.base.growth_11_20 * 100).toFixed(2)}%
                </span>
                <span className="ml-1 text-obsidian-muted">(terminal)</span>
              </span>
              <span className="text-slate-300">
                Discount{' '}
                <span className="text-slate-100">
                  {((valuation.discount_rate ?? 0) * 100).toFixed(2)}%
                </span>
              </span>
            </div>
          </div>
        )}

        {/* ---------------- fetched vs yours ---------------- */}
        <div className="px-5 py-4">
          <div className="mb-2 grid grid-cols-[1fr_110px_130px] gap-3 text-[10px] uppercase tracking-wide text-obsidian-muted">
            <span>Input</span>
            <span className="text-right">
              Fetched
              {auto?.source && (
                <span className="ml-1 normal-case text-slate-500">({auto.source})</span>
              )}
            </span>
            <span className="text-right">Your override</span>
          </div>

          <div className="space-y-1.5">
            {FIELDS.map((field) => {
              const fetched = auto?.[field.key as keyof typeof auto] as
                | number
                | null
                | undefined;
              const isOverridden = overridden.has(field.key as string);
              return (
                <div
                  key={field.key}
                  className="grid grid-cols-[1fr_110px_130px] items-center gap-3"
                >
                  <div>
                    <div className="text-[11px] text-slate-300">{field.label}</div>
                    {/* Statement FX's static hint explains WHEN it matters;
                        this replaces it with the specific fetched currency
                        once known, so "differs from USD" becomes "ASML
                        files in EUR" rather than staying generic. */}
                    {field.key === 'statement_exchange_rate' &&
                    auto?.statement_currency &&
                    auto.statement_currency !== 'USD' ? (
                      <div className="text-[10px] text-amber-400/80">
                        Files in {auto.statement_currency} -- required to value this holding
                      </div>
                    ) : /* The provider told us nothing. FMP returns no
                          statements at all for a foreign private issuer, so
                          there is no reported currency to name -- and
                          assuming USD is what read ASML's EUR figures as
                          dollars. Asked for rather than guessed. */
                    field.key === 'statement_exchange_rate' &&
                      auto !== null &&
                      auto !== undefined &&
                      !auto.statement_currency &&
                      auto.statement_exchange_rate === null ? (
                      <div className="text-[10px] text-amber-400/80">
                        Filing currency unknown -- enter 1 USD in whatever
                        currency this company reports in
                      </div>
                    ) : (
                      field.hint && (
                        <div className="text-[10px] text-slate-600">{field.hint}</div>
                      )
                    )}
                  </div>

                  {/* Read-only on purpose: this column is the record of what the
                      provider said, and editing it in place would destroy the
                      only baseline a drifting assumption can be seen against. */}
                  <div
                    className={`text-right font-mono text-[11px] ${
                      isOverridden ? 'text-slate-600 line-through' : 'text-slate-400'
                    }`}
                  >
                    {display(fetched, field.percent)}
                  </div>

                  <input
                    type="number"
                    step="any"
                    value={draft[field.key] ?? ''}
                    placeholder={field.percent ? '%' : '—'}
                    onChange={(e) =>
                      setDraft((prev) => ({ ...prev, [field.key]: e.target.value }))
                    }
                    className="w-full rounded border border-obsidian-border bg-obsidian-bg px-2 py-1 text-right font-mono text-[11px] text-slate-100 placeholder:text-slate-700 focus:border-slate-600 focus:outline-none"
                  />
                </div>
              );
            })}
          </div>

          <p className="mt-3 text-[10px] text-obsidian-muted">
            A blank box means “use the fetched value”. The monthly refresh
            replaces the fetched column and never touches yours.
          </p>
        </div>

        {error && (
          <div className="mx-5 mb-3 flex items-start gap-1.5 rounded border border-loss/30 bg-loss/5 px-3 py-2 text-[11px] text-loss">
            <AlertCircle className="mt-px h-3.5 w-3.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        {/* ---------------- actions ---------------- */}
        <div className="flex items-center justify-between gap-2 border-t border-obsidian-border px-5 py-3">
          <button
            type="button"
            disabled={!override || clear.isPending}
            title="Discard every override and return to the fetched values"
            onClick={() => {
              setError(null);
              clear.mutate(holding.ticker, {
                onSuccess: onClose,
                onError: (e) => setError(e.message),
              });
            }}
            className="inline-flex items-center gap-1.5 rounded-lg border border-obsidian-border px-3 py-1.5 text-[11px] text-obsidian-muted transition-colors hover:border-slate-600 hover:text-slate-200 disabled:opacity-40"
          >
            {clear.isPending ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <RotateCcw className="h-3 w-3" />
            )}
            Reset to fetched
          </button>

          <div className="flex gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg border border-obsidian-border px-3 py-1.5 text-[11px] text-obsidian-muted transition-colors hover:text-slate-200"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={save.isPending}
              onClick={submit}
              className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-4 py-1.5 text-[11px] font-medium text-emerald-300 transition-colors hover:bg-emerald-500/20 disabled:opacity-50"
            >
              {save.isPending && <Loader2 className="h-3 w-3 animate-spin" />}
              Save and revalue
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

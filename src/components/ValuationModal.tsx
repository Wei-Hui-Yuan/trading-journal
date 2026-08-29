'use client';

import React, { useMemo, useState } from 'react';
import { AlertCircle, Loader2, RotateCcw, X } from 'lucide-react';

import {
  useClearValuationOverride,
  useSetValuationOverride,
} from '@/hooks/useInvestments';
import type { Holding, ValuationOverridePayload } from '@/types/investments';

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
  { key: 'base_flow', label: 'Free cash flow', hint: 'millions, as reported' },
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
              Twenty-year discounted cash flow · no terminal value
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
                {valuation.premium_pct === null || valuation.premium_pct === undefined
                  ? '—'
                  : `${valuation.premium_pct < 0 ? '−' : '+'}${Math.abs(
                      valuation.premium_pct
                    ).toFixed(1)}%`}
              </div>
            </div>
          </div>
        ) : (
          <div className="flex items-start gap-2 border-b border-obsidian-border bg-amber-500/5 px-5 py-3 text-[11px] text-amber-300">
            <AlertCircle className="mt-px h-3.5 w-3.5 shrink-0" />
            <span>
              Not valued yet — needs{' '}
              <span className="font-mono">
                {(valuation?.missing ?? ['inputs']).join(', ')}
              </span>
              . Fill them in on the right and it will value on save.
            </span>
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

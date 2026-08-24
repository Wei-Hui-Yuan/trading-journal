'use client';

import React, { useEffect, useState } from 'react';
import {
  AlertCircle,
  Check,
  Database,
  Download,
  FileSpreadsheet,
  GhostIcon,
  Loader2,
  Wallet,
} from 'lucide-react';

import {
  useSettings,
  useSuppressedExecutions,
  useUpdateSettings,
} from '@/hooks/useTradeInbox';
import { useExportCsv } from '@/hooks/useCsvExport';
import { SuppressedFillsModal } from '@/components/SuppressedFillsModal';
import type { ExportDataset } from '@/types/api';

/**
 * Held as strings for the same reason the trade form does: a controlled number
 * input must be able to represent "empty" and mid-typing states that Number()
 * would turn into NaN.
 */
interface Draft {
  accountSize: string;
  riskPercent: string;
}

const toNullableNumber = (raw: string): number | null => {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
};

const money = (n: number) =>
  n.toLocaleString('en-US', { style: 'currency', currency: 'USD' });

/**
 * The four exports, in the order they are offered.
 *
 * Analysis grain first in each pair, because that is the one wanted most of the
 * time. The ledger grain is described as the irreplaceable one rather than the
 * detailed one -- "every fill" undersells why it matters, which is that nothing
 * else in the system can reconstruct those rows.
 */
const EXPORTS: {
  dataset: ExportDataset;
  label: string;
  detail: string;
  ledger: boolean;
}[] = [
  {
    dataset: 'round-trips',
    label: 'Trading — round trips',
    detail: 'One row per trade idea: plan, outcome, R, grade and review.',
    ledger: false,
  },
  {
    dataset: 'executions',
    label: 'Trading — executions',
    detail: 'Every fill exactly as stored. Cannot be rebuilt from anything else.',
    ledger: true,
  },
  {
    dataset: 'investment-holdings',
    label: 'Investing — holdings',
    detail: 'One row per holding: cost, price, value, weight and intrinsic value.',
    ledger: false,
  },
  {
    dataset: 'investment-transactions',
    label: 'Investing — transactions',
    detail: 'Every transaction as stored. The whole book is derived from these.',
    ledger: true,
  },
];

export default function SettingsPage() {
  const settingsQuery = useSettings();
  const saveMutation = useUpdateSettings();

  const [draft, setDraft] = useState<Draft>({ accountSize: '', riskPercent: '' });
  const [error, setError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  // Whether the user has begun editing. Until they have, the draft tracks the
  // server; after, it is theirs — otherwise a background refetch would
  // overwrite half-typed input.
  const [touched, setTouched] = useState(false);
  const [suppressedOpen, setSuppressedOpen] = useState(false);

  // Fetched eagerly so the count is on the button before it is pressed: a
  // non-zero badge is the only hint that anything is being skipped at all.
  const suppressedQuery = useSuppressedExecutions();
  const suppressedCount = suppressedQuery.data?.length ?? 0;

  // One mutation behind four buttons. `variables` names the dataset in flight,
  // so only the button that was clicked shows a spinner.
  const exportMutation = useExportCsv();
  const exporting = exportMutation.isPending ? exportMutation.variables : null;

  const settings = settingsQuery.data;

  useEffect(() => {
    if (!settings || touched) return;
    setDraft({
      accountSize: settings.account_size === null ? '' : String(settings.account_size),
      riskPercent: String(settings.risk_percent),
    });
  }, [settings, touched]);

  const patch = (p: Partial<Draft>) => {
    setTouched(true);
    setDraft((prev) => ({ ...prev, ...p }));
    setError(null);
    setSavedAt(null);
  };

  const accountSize = toNullableNumber(draft.accountSize);
  const riskPercent = toNullableNumber(draft.riskPercent);

  // The figure the calculator will actually spend per trade. Shown here so the
  // consequence of a change is visible before saving, not discovered later in
  // the middle of sizing a live trade.
  const riskPerTrade =
    accountSize !== null && riskPercent !== null
      ? (accountSize * riskPercent) / 100
      : null;

  const handleSave = (e: React.FormEvent) => {
    e.preventDefault();

    if (draft.accountSize.trim() !== '' && accountSize === null)
      return setError('Account size must be a number.');
    if (accountSize !== null && accountSize < 0)
      return setError('Account size cannot be negative.');
    // Blank is rejected rather than coerced: risk_percent is NOT NULL
    // server-side, and silently substituting a default would change how every
    // future trade is sized without saying so.
    if (riskPercent === null)
      return setError('Risk per trade is required.');
    if (riskPercent <= 0) return setError('Risk per trade must be greater than zero.');
    if (riskPercent > 100) return setError('Risk per trade cannot exceed 100%.');

    saveMutation.mutate(
      { account_size: accountSize, risk_percent: riskPercent },
      {
        onSuccess: () => {
          setSavedAt(Date.now());
          // Hand control back to the server value now that it matches.
          setTouched(false);
        },
        onError: (err) => setError(err.message),
      }
    );
  };

  const fieldClass =
    'w-full rounded-lg bg-obsidian-bg border border-obsidian-border px-3 py-2 text-sm text-slate-200 ' +
    'placeholder:text-obsidian-muted focus:outline-none focus:border-slate-600 transition-colors ' +
    'disabled:opacity-50';

  const isSaving = saveMutation.isPending;

  return (
    <div className="min-h-screen flex flex-col bg-obsidian-bg">
      <main className="flex-1 max-w-2xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <section className="rounded-xl border border-obsidian-border bg-obsidian-card p-5">
          <div className="flex items-center gap-2 mb-1">
            <Wallet className="h-4 w-4 text-win" />
            <h2 className="text-sm font-semibold tracking-wide text-slate-200">
              RISK &amp; POSITION SIZING
            </h2>
          </div>
          <p className="text-xs text-obsidian-muted mb-5">
            The Manual Log calculator reads these automatically — you should not
            have to retype them per trade.
          </p>

          {settingsQuery.isPending && (
            <div className="flex items-center py-6 text-obsidian-muted">
              <Loader2 className="h-4 w-4 animate-spin mr-2" />
              <span className="text-xs">Loading…</span>
            </div>
          )}

          {settingsQuery.isError && (
            <div className="flex items-start py-4 text-loss text-xs">
              <AlertCircle className="h-4 w-4 mr-1.5 shrink-0" />
              <span>
                {settingsQuery.error instanceof Error
                  ? settingsQuery.error.message
                  : 'Failed to load settings.'}
              </span>
            </div>
          )}

          {!settingsQuery.isPending && !settingsQuery.isError && (
            <form onSubmit={handleSave} className="space-y-5">
              <label className="block">
                <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
                  Account Size
                </span>
                <input
                  type="number"
                  inputMode="decimal"
                  step="0.01"
                  min="0"
                  value={draft.accountSize}
                  onChange={(e) => patch({ accountSize: e.target.value })}
                  disabled={isSaving}
                  placeholder="2500"
                  className={`mt-1 font-mono ${fieldClass}`}
                />
                <span className="mt-1.5 block text-[11px] text-obsidian-muted">
                  Net liquidation value. Update it occasionally as the account
                  grows — trades already logged keep the risk they were sized
                  with, so changing this never rewrites your history.
                </span>
              </label>

              <label className="block">
                <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
                  Default Risk Per Trade (%)
                </span>
                {/* 0.01, matching app_settings.risk_percent's NUMERIC(5,2).
                    Precision matters more here than on a plan: this value is
                    stored exactly as typed, where a plan's is recomputed from
                    the share count before saving. `any` would let 1.234
                    through and the column would round it to 1.23 silently. */}
                <input
                  type="number"
                  inputMode="decimal"
                  step="0.01"
                  min="0"
                  value={draft.riskPercent}
                  onChange={(e) => patch({ riskPercent: e.target.value })}
                  disabled={isSaving}
                  placeholder="1"
                  className={`mt-1 font-mono ${fieldClass}`}
                />
                <span className="mt-1.5 block text-[11px] text-obsidian-muted">
                  The starting value in the calculator. It stays editable per
                  trade, so a lower-conviction setup can be sized smaller
                  without changing this.
                </span>
              </label>

              {riskPerTrade !== null && (
                <div className="rounded-lg border border-obsidian-border bg-obsidian-bg/40 px-3 py-2.5 text-xs">
                  <span className="text-obsidian-muted">Risk budget per trade</span>
                  <span className="ml-2 font-mono text-slate-200">
                    {money(riskPerTrade)}
                  </span>
                  <span className="ml-1 text-obsidian-muted">
                    ({riskPercent}% of {money(accountSize as number)})
                  </span>
                </div>
              )}

              {error && (
                <div className="flex items-start text-xs text-loss">
                  <AlertCircle className="h-3.5 w-3.5 mr-1.5 mt-px shrink-0" />
                  <span>{error}</span>
                </div>
              )}

              {savedAt !== null && (
                <div className="flex items-start text-xs text-win">
                  <Check className="h-3.5 w-3.5 mr-1.5 mt-px shrink-0" />
                  <span>Saved. New trades will size against this.</span>
                </div>
              )}

              <button
                type="submit"
                disabled={isSaving}
                className="inline-flex items-center gap-2 rounded-lg border border-win-border bg-win-glow px-4 py-2 text-xs font-medium text-win hover:bg-win/20 disabled:opacity-60 disabled:cursor-not-allowed transition-colors"
              >
                {isSaving ? (
                  <>
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    Saving…
                  </>
                ) : (
                  <>
                    <Check className="h-3.5 w-3.5" />
                    Save Settings
                  </>
                )}
              </button>
            </form>
          )}
        </section>

        {/* Sits in Settings rather than the ledger because a suppressed fill
            is not in the ledger — that is what suppression means. Nothing on
            any other page can show it. */}
        <section className="mt-6 rounded-xl border border-obsidian-border bg-obsidian-card p-5">
          <div className="mb-1 flex items-center gap-2">
            <GhostIcon className="h-4 w-4 text-amber-400" />
            <h2 className="text-sm font-semibold tracking-wide text-slate-200">
              BROKER SYNC
            </h2>
          </div>
          <p className="mb-4 text-xs text-obsidian-muted">
            Fills you deleted are suppressed so a later sync cannot add them
            back. Review that list here if a trade you expected never arrived.
          </p>
          <button
            type="button"
            onClick={() => setSuppressedOpen(true)}
            className="inline-flex items-center gap-2 rounded-lg border border-obsidian-border bg-obsidian-bg px-3.5 py-2 text-xs font-medium text-obsidian-muted transition-colors hover:border-slate-600 hover:text-slate-200"
          >
            <GhostIcon className="h-3.5 w-3.5" />
            Manage Suppressed Fills
            {suppressedCount > 0 && (
              <span className="rounded bg-amber-500/15 px-1.5 py-0.5 font-mono text-[10px] text-amber-300">
                {suppressedCount}
              </span>
            )}
          </button>
        </section>

        {/* Sits in Settings because it is about the data as a whole rather than
            about any one trade -- and because a backup is a housekeeping act,
            not part of the journalling loop. */}
        <section className="mt-6 rounded-xl border border-obsidian-border bg-obsidian-card p-5">
          <div className="mb-1 flex items-center gap-2">
            <Download className="h-4 w-4 text-sky-400" />
            <h2 className="text-sm font-semibold tracking-wide text-slate-200">
              EXPORT YOUR DATA
            </h2>
          </div>
          <p className="mb-4 max-w-2xl text-xs text-obsidian-muted">
            Two files per book. The first of each pair is for analysis — the rows
            the pages show, ready to pivot in a spreadsheet. The second is the
            ledger exactly as stored, which is the one worth keeping somewhere
            else: nothing in this app can rebuild those rows if the database is
            lost.
          </p>

          <div className="grid gap-2 sm:grid-cols-2">
            {EXPORTS.map(({ dataset, label, detail, ledger }) => {
              const busy = exporting === dataset;
              return (
                <button
                  key={dataset}
                  type="button"
                  onClick={() => exportMutation.mutate(dataset)}
                  disabled={exportMutation.isPending}
                  aria-busy={busy}
                  className="group flex items-start gap-3 rounded-lg border border-obsidian-border bg-obsidian-bg p-3 text-left transition-colors hover:border-slate-600 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  <span className="mt-0.5 shrink-0 text-obsidian-muted group-hover:text-slate-200">
                    {busy ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : ledger ? (
                      <Database className="h-4 w-4" />
                    ) : (
                      <FileSpreadsheet className="h-4 w-4" />
                    )}
                  </span>
                  <span className="min-w-0">
                    <span className="block text-xs font-medium text-slate-200">
                      {label}
                    </span>
                    <span className="mt-0.5 block text-[11px] leading-snug text-obsidian-muted">
                      {busy ? 'Preparing…' : detail}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>

          {/* Shown rather than swallowed. A download that silently does nothing
              is indistinguishable from one the browser blocked. */}
          {exportMutation.isError && (
            <div className="mt-3 flex items-start gap-2 rounded-lg border border-loss-border bg-loss-glow px-3 py-2 text-xs text-loss">
              <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{exportMutation.error.message}</span>
            </div>
          )}
        </section>
      </main>

      <SuppressedFillsModal
        open={suppressedOpen}
        onClose={() => setSuppressedOpen(false)}
      />
    </div>
  );
}

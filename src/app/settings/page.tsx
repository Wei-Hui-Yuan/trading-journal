'use client';

import React, { useEffect, useState } from 'react';
import Link from 'next/link';
import {
  AlertCircle,
  ArrowLeft,
  Check,
  Loader2,
  SlidersHorizontal,
  Wallet,
} from 'lucide-react';

import { useSettings, useUpdateSettings } from '@/hooks/useTradeInbox';

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
      <header className="border-b border-obsidian-border bg-obsidian-card/80 backdrop-blur-md sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <div className="h-9 w-9 rounded-xl bg-gradient-to-br from-win/20 to-emerald-900/40 border border-win/30 flex items-center justify-center">
              <SlidersHorizontal className="h-5 w-5 text-win" />
            </div>
            <div>
              <span className="font-bold text-lg tracking-wider text-white">
                SETTINGS
              </span>
              <p className="text-xs text-obsidian-muted font-medium">
                Defaults every new trade is sized against
              </p>
            </div>
          </div>

          <Link
            href="/"
            className="inline-flex items-center gap-2 rounded-lg bg-obsidian-bg border border-obsidian-border px-3 py-2 text-xs font-medium text-obsidian-muted hover:text-slate-100 hover:border-slate-600 transition-colors"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            Dashboard
          </Link>
        </div>
      </header>

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
                <input
                  type="number"
                  inputMode="decimal"
                  step="0.05"
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
      </main>
    </div>
  );
}

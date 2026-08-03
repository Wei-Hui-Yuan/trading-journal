'use client';

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import Link from 'next/link';
import { AlertCircle, Calculator, Check, ClipboardList, Loader2, X } from 'lucide-react';

import {
  useCreatePlan,
  useSettings,
  useStrategies,
  useUploadPlanChart,
} from '@/hooks/useTradeInbox';
import { ChartDropzone } from '@/components/PlanChart';
import { computeSizing, scoreTakeProfit, sizingHint } from '@/lib/positionSizing';
import type { CompressedChart } from '@/lib/chartImage';
import type { TradeSide } from '@/types/api';

interface CreatePlanModalProps {
  open: boolean;
  onClose: () => void;
}

/**
 * All numeric fields are held as strings.
 *
 * A controlled number input must be able to represent "empty" and mid-typing
 * states ("1.", "-") that Number() would mangle into NaN. They are parsed once,
 * at submit, by `toNullableNumber`.
 */
interface FormState {
  symbol: string;
  side: TradeSide;
  quantity: string;
  // The plan
  plannedEntry: string;
  plannedStopLoss: string;
  takeProfitPrice: string;
  // The idea
  strategyId: string; // '' means none chosen
  thesis: string;
  /**
   * Risk for THIS trade, seeded from the saved default but editable — a
   * lower-conviction setup gets sized smaller without changing the default.
   *
   * Account size is deliberately absent: it belongs to the account, not to a
   * trade, so it is read from Settings rather than retyped here.
   */
  riskPercent: string;
}

/** '' / whitespace / unparseable -> null, so the API never receives NaN. */
function toNullableNumber(raw: string): number | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
}

/** Payload key -> the form field it came from, for targeted error messages. */
const fieldForKey = {
  planned_entry: 'plannedEntry',
  stop_loss: 'plannedStopLoss',
  take_profit: 'takeProfitPrice',
} as const satisfies Record<string, keyof FormState>;

const blankForm = (): FormState => ({
  symbol: '',
  side: 'BUY',
  quantity: '',
  plannedEntry: '',
  plannedStopLoss: '',
  takeProfitPrice: '',
  strategyId: '',
  thesis: '',
  riskPercent: '',
});

/** Money, to the cent. */
const money = (n: number) =>
  n.toLocaleString('en-US', { style: 'currency', currency: 'USD' });

/**
 * A price, at the precision the instrument warrants.
 *
 * Sub-dollar tickers need more than two decimals or every R target rounds to
 * the same number and the ladder reads as though it has no spacing.
 */
const price = (n: number) => (n < 1 ? n.toFixed(4) : n.toFixed(2));

/**
 * Everything you decide before entering a trade — and nothing you cannot know yet.
 *
 * This was the Manual Log form. It had a REQUIRED actual-entry field, so
 * recording a plan meant typing a fill price for a trade that had not
 * executed. One real PANW plan was saved with its actual entry set to its
 * take-profit for exactly that reason, leaving the journal carrying an open
 * position that was never bought.
 *
 * Worse, a hand-logged fill and IBKR's copy of the same execution shared no
 * identifier — `trades` deduplicates on the broker's id — so planning here and
 * then syncing produced two rows for one real trade: double the position,
 * wrong average cost, phantom shares left open after the real ones were sold.
 *
 * A plan now goes to `planned_trades` instead. It cannot reach P&L, win rate
 * or exposure, and the collision it used to cause is no longer expressible.
 * The sizing calculator is unchanged: working out what to risk is planning,
 * which is what this form was always really for.
 */
export function CreatePlanModal({ open, onClose }: CreatePlanModalProps) {
  const [form, setForm] = useState<FormState>(blankForm);
  const [error, setError] = useState<string | null>(null);
  const [savedSummary, setSavedSummary] = useState<string | null>(null);
  const [mounted, setMounted] = useState(false);
  // Held rather than uploaded on selection: the upload is keyed by plan id,
  // and there is no id until the plan itself is saved. Compressed already
  // though, so the size shown is the size that will be stored.
  const [chart, setChart] = useState<CompressedChart | null>(null);
  const uploadChart = useUploadPlanChart();
  const symbolRef = useRef<HTMLInputElement>(null);

  const mutation = useCreatePlan();
  // Offered straight from the playbook, so the two cannot drift apart.
  const { data: strategies } = useStrategies();
  // Read-only here. Editing lives on /settings so account size is set
  // occasionally rather than retyped for every trade.
  const { data: settings } = useSettings();

  // The portal target only exists in the browser.
  useEffect(() => setMounted(true), []);

  useEffect(() => {
    if (open) {
      setForm(blankForm());
      setError(null);
      setSavedSummary(null);
      // Or the previous plan's screenshot would be attached to the next one.
      setChart(null);
      // Focus the first field so the form is keyboard-ready.
      window.setTimeout(() => symbolRef.current?.focus(), 0);
    }
  }, [open]);

  // Seed the per-trade risk from the saved default once settings arrive.
  // Guarded on the field being untouched, because settings can resolve after
  // the user has started typing and overwriting mid-keystroke would be worse
  // than not prefilling.
  useEffect(() => {
    if (!open || !settings) return;
    setForm((prev) =>
      prev.riskPercent === ''
        ? { ...prev, riskPercent: String(settings.risk_percent) }
        : prev
    );
  }, [open, settings]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !mutation.isPending) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose, mutation.isPending]);

  // The calculator reads the plan rather than duplicating it. Entry and stop
  // are the same two numbers the form already collects; a second copy of them
  // could disagree with the first, and you would size against one while
  // recording the other.
  const accountSize = settings?.account_size ?? null;
  const sizingInputs = useMemo(
    () => ({
      side: form.side,
      entry: toNullableNumber(form.plannedEntry),
      stop: toNullableNumber(form.plannedStopLoss),
      accountSize,
      riskPercent: toNullableNumber(form.riskPercent),
    }),
    [form.side, form.plannedEntry, form.plannedStopLoss, accountSize, form.riskPercent]
  );
  const sizing = useMemo(() => computeSizing(sizingInputs), [sizingInputs]);
  const hint = useMemo(() => sizingHint(sizingInputs), [sizingInputs]);

  // Dollars at risk on the quantity actually planned, which is not necessarily
  // the quantity the calculator suggested — taking half size is a deliberate
  // act and the plan should record it as half the risk.
  const enteredQty = toNullableNumber(form.quantity);
  const plannedRisk =
    sizing !== null && enteredQty !== null && enteredQty > 0
      ? enteredQty * sizing.riskPerShare
      : null;
  const plannedRiskPercent =
    plannedRisk !== null && accountSize ? (plannedRisk / accountSize) * 100 : null;

  // What the take profit actually typed into the form is worth, as opposed to
  // the 1R/2R/3R chips. Sized on the quantity being planned where one has been
  // entered, falling back to the calculator's suggestion — the same share count
  // the ladder prices its own targets on, so the two are comparable.
  const takeProfitScore = useMemo(() => {
    const entry = toNullableNumber(form.plannedEntry);
    const target = toNullableNumber(form.takeProfitPrice);
    if (sizing === null || entry === null || target === null) return null;
    return scoreTakeProfit({
      side: form.side,
      entry,
      takeProfit: target,
      riskPerShare: sizing.riskPerShare,
      shares: enteredQty ?? sizing.wholeShares,
    });
  }, [form.plannedEntry, form.takeProfitPrice, form.side, sizing, enteredQty]);

  if (!open || !mounted) return null;

  const patch = (patchObj: Partial<FormState>) => {
    setForm((prev) => ({ ...prev, ...patchObj }));
    setError(null);
    setSavedSummary(null);
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();

    const symbol = form.symbol.trim().toUpperCase();

    if (!symbol) return setError('Ticker is required.');
    if (symbol.length > 10) return setError('Ticker must be 10 characters or fewer.');

    // Everything else is optional. A plan is worth recording the moment you
    // have a ticker and a bias — requiring the full price triangle is what
    // pushed people into inventing numbers to get the form to save.
    const quantity = toNullableNumber(form.quantity);
    if (quantity === null && form.quantity.trim() !== '')
      return setError('Quantity must be a number.');
    if (quantity !== null && quantity <= 0)
      return setError('Quantity must be greater than zero.');

    const optional = {
      planned_entry: toNullableNumber(form.plannedEntry),
      stop_loss: toNullableNumber(form.plannedStopLoss),
      take_profit: toNullableNumber(form.takeProfitPrice),
    };
    const labels: Record<keyof typeof optional, string> = {
      planned_entry: 'Planned entry',
      stop_loss: 'Planned stop loss',
      take_profit: 'Take profit price',
    };
    for (const [key, value] of Object.entries(optional)) {
      const typedKey = key as keyof typeof optional;
      // Non-empty but unparseable, or non-positive.
      if (value === null && form[fieldForKey[typedKey]].trim() !== '')
        return setError(`${labels[typedKey]} must be a number.`);
      if (value !== null && value <= 0)
        return setError(`${labels[typedKey]} must be greater than zero.`);
    }

    mutation.mutate(
      {
        ticker: symbol,
        direction: form.side,
        quantity,
        strategy_id: form.strategyId || null,
        thesis: form.thesis.trim() || null,
        // Only sent when a stop makes them meaningful. Without one there is no
        // risk per share, so any figure here would be invented rather than
        // measured — and a null is honest where a zero would not be.
        risk_amount: plannedRisk,
        risk_percent: plannedRiskPercent,
        ...optional,
      },
      {
        onSuccess: async (plan) => {
          const saved =
            `Plan saved for ${plan.ticker}. It will attach itself to the fill ` +
            'when your next broker sync brings it in — nothing has been ' +
            'added to the ledger.';

          // The plan is already saved at this point, so a failed chart upload
          // must not read as a failed save. It is reported as what it is --
          // the plan kept, the screenshot not attached -- and the modal stays
          // open so the image can be retried rather than silently lost.
          if (chart) {
            try {
              await uploadChart.mutateAsync({
                planId: plan.id,
                image: chart.blob,
                filename: `${plan.ticker}-chart.${chart.mime === 'image/webp' ? 'webp' : 'png'}`,
              });
            } catch (err) {
              setError(
                `${saved} The chart could not be attached: ` +
                  `${err instanceof Error ? err.message : 'upload failed'}`
              );
              return;
            }
          }

          setSavedSummary(saved);
          // Held open a moment so the outcome is readable.
          window.setTimeout(onClose, 2200);
        },
        onError: (err) => setError(err.message),
      }
    );
  };

  const fieldClass =
    'w-full rounded-lg bg-obsidian-bg border border-obsidian-border px-3 py-2 text-sm text-slate-200 ' +
    'placeholder:text-obsidian-muted focus:outline-none focus:border-slate-600 transition-colors ' +
    'disabled:opacity-50';

  const isSaving = mutation.isPending;

  // Rendered through a portal on <body>. The header that mounts this button
  // uses backdrop-blur, which creates a containing block for fixed-position
  // descendants -- inside it, `fixed inset-0` would resolve against the 64px
  // header instead of the viewport and clip the dialog off-screen.
  return createPortal(
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Create a trade plan"
    >
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/70 backdrop-blur-sm"
        onClick={() => !isSaving && onClose()}
      />

      {/* Wider and taller than a plain entry form needs, because the sizing
          section is meant to be read alongside the plan it consumes rather
          than scrolled to. */}
      <div className="relative w-full max-w-xl rounded-xl border border-obsidian-border bg-obsidian-card shadow-2xl">
        <div className="flex items-center justify-between px-5 py-4 border-b border-obsidian-border">
          <div className="flex items-center gap-2">
            <ClipboardList className="h-4 w-4 text-amber-400" />
            <div>
              <h2 className="text-sm font-semibold tracking-wide text-slate-100">
                CREATE TRADE PLAN
              </h2>
              <p className="text-[10px] text-obsidian-muted">
                Before you enter. No fill price — the broker supplies that.
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={isSaving}
            aria-label="Close"
            className="p-1 rounded-lg text-obsidian-muted hover:text-slate-200 hover:bg-obsidian-bg transition-colors disabled:opacity-50"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <form
          onSubmit={handleSubmit}
          className="px-5 py-5 space-y-4 max-h-[82vh] overflow-y-auto"
        >
          {/* ---- Section 1: core details ---- */}
          <label className="block">
            <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
              Ticker
            </span>
            <input
              ref={symbolRef}
              type="text"
              value={form.symbol}
              // Uppercase as the user types, so what they see is what is sent.
              onChange={(e) => patch({ symbol: e.target.value.toUpperCase() })}
              disabled={isSaving}
              maxLength={10}
              placeholder="AAPL"
              className={`mt-1 font-mono uppercase ${fieldClass}`}
            />
          </label>

          <div>
            <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
              Direction
            </span>
            <div className="mt-1 grid grid-cols-2 gap-2">
              {(['BUY', 'SELL'] as const).map((side) => {
                const active = form.side === side;
                const activeClass =
                  side === 'BUY'
                    ? 'border-win/50 bg-win/15 text-win'
                    : 'border-loss/50 bg-loss/15 text-loss';
                return (
                  <button
                    key={side}
                    type="button"
                    onClick={() => patch({ side })}
                    disabled={isSaving}
                    aria-pressed={active}
                    className={`rounded-lg border px-3 py-2 text-xs font-semibold transition-colors disabled:opacity-50 ${
                      active
                        ? activeClass
                        : 'border-obsidian-border bg-obsidian-bg text-obsidian-muted hover:text-slate-200 hover:border-slate-600'
                    }`}
                  >
                    {side === 'BUY' ? 'Long / Buy' : 'Short / Sell'}
                  </button>
                );
              })}
            </div>
          </div>

          <label className="block">
            <span className="text-[11px] uppercase tracking-wider text-obsidian-muted">
              Planned Quantity
            </span>
            <input
              type="number"
              inputMode="numeric"
              step="1"
              min="1"
              value={form.quantity}
              onChange={(e) => patch({ quantity: e.target.value })}
              disabled={isSaving}
              placeholder="100"
              className={`mt-1 font-mono ${fieldClass}`}
            />
            <span className="mt-1.5 block text-[10px] text-obsidian-muted">
              What you intend to take. The fill you actually get is whatever
              IBKR reports — often split across several executions.
            </span>
          </label>

          {/* ---- Section 2: the plan ---- */}
          <fieldset className="rounded-lg border border-obsidian-border bg-obsidian-bg/40 px-3 pb-3 pt-2">
            <legend className="px-1.5 text-[10px] font-semibold uppercase tracking-wider text-obsidian-muted">
              The Plan · Risk Setup
            </legend>
            <div className="grid grid-cols-3 gap-2">
              <label className="block">
                <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
                  Plan Entry
                </span>
                <input
                  type="number"
                  inputMode="decimal"
                  step="0.0001"
                  min="0"
                  value={form.plannedEntry}
                  onChange={(e) => patch({ plannedEntry: e.target.value })}
                  disabled={isSaving}
                  placeholder="150.00"
                  className={`mt-1 font-mono text-xs ${fieldClass}`}
                />
              </label>

              <label className="block">
                <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
                  Plan Stop
                </span>
                <input
                  type="number"
                  inputMode="decimal"
                  step="0.0001"
                  min="0"
                  value={form.plannedStopLoss}
                  onChange={(e) => patch({ plannedStopLoss: e.target.value })}
                  disabled={isSaving}
                  placeholder="148.00"
                  className={`mt-1 font-mono text-xs ${fieldClass}`}
                />
              </label>

              <label className="block">
                <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
                  Take Profit
                </span>
                <input
                  type="number"
                  inputMode="decimal"
                  step="0.0001"
                  min="0"
                  value={form.takeProfitPrice}
                  onChange={(e) => patch({ takeProfitPrice: e.target.value })}
                  disabled={isSaving}
                  placeholder="156.00"
                  className={`mt-1 font-mono text-xs ${fieldClass}`}
                />
              </label>
            </div>
            <p className="mt-1.5 text-[10px] text-obsidian-muted">
              Entry and stop drive the sizing below. All optional — a ticker and
              a direction is enough to save a plan.
            </p>
          </fieldset>

          {/* ---- Section 2b: position sizing ----
              Sits directly under the plan because it consumes it: entry and
              stop above are its only price inputs. */}
          <fieldset className="rounded-lg border border-obsidian-border bg-obsidian-bg/40 px-3 pb-3 pt-2">
            <legend className="flex items-center gap-1.5 px-1.5 text-[10px] font-semibold uppercase tracking-wider text-obsidian-muted">
              <Calculator className="h-3 w-3" />
              Position Sizing
            </legend>

            <div className="grid grid-cols-2 gap-2">
              {/* Read-only, from Settings. Shown rather than hidden so it is
                  obvious which balance the sizing below is against. */}
              <div>
                <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
                  Account Size
                </span>
                <div className="mt-1 flex h-[38px] items-center justify-between rounded-lg border border-obsidian-border bg-obsidian-bg/60 px-3">
                  <span className="font-mono text-xs text-slate-300">
                    {accountSize === null ? 'not set' : money(accountSize)}
                  </span>
                  <Link
                    href="/settings"
                    className="text-[10px] text-obsidian-muted hover:text-slate-200 transition-colors"
                  >
                    Edit
                  </Link>
                </div>
              </div>

              <label className="block">
                <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
                  Risk % This Trade
                </span>
                <input
                  type="number"
                  inputMode="decimal"
                  step="0.05"
                  min="0"
                  value={form.riskPercent}
                  onChange={(e) => patch({ riskPercent: e.target.value })}
                  disabled={isSaving}
                  placeholder="1"
                  className={`mt-1 font-mono text-xs ${fieldClass}`}
                />
              </label>
            </div>

            {accountSize === null && (
              <p className="mt-2 text-[10px] text-obsidian-muted">
                <Link href="/settings" className="text-slate-300 underline">
                  Set your account size
                </Link>{' '}
                to get a share count. Target prices work without it.
              </p>
            )}

            {/* Outputs. A reason is shown rather than an empty panel — an
                inverted stop is a mistake worth naming, not hiding. */}
            {sizing === null ? (
              <p className="mt-2.5 text-[10px] text-obsidian-muted">{hint}</p>
            ) : (
              <div className="mt-2.5 space-y-2.5">
                <div className="grid grid-cols-2 gap-x-3 gap-y-1.5 text-[11px]">
                  <div className="flex justify-between">
                    <span className="text-obsidian-muted">1R / share</span>
                    <span className="font-mono text-slate-200">
                      {money(sizing.riskPerShare)}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-obsidian-muted">Risk budget</span>
                    <span className="font-mono text-slate-200">
                      {sizing.riskAmount === null ? '—' : money(sizing.riskAmount)}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-obsidian-muted">Shares</span>
                    <span className="font-mono text-slate-200">
                      {sizing.wholeShares === null ? '—' : sizing.wholeShares}
                      {sizing.exactShares !== null && (
                        <span className="ml-1 text-obsidian-muted">
                          ({sizing.exactShares.toFixed(2)})
                        </span>
                      )}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-obsidian-muted">Cost</span>
                    <span className="font-mono text-slate-200">
                      {sizing.positionCost === null ? '—' : money(sizing.positionCost)}
                      {sizing.accountFraction !== null && (
                        <span
                          className={`ml-1 ${
                            sizing.accountFraction > 1 ? 'text-loss' : 'text-obsidian-muted'
                          }`}
                        >
                          ({(sizing.accountFraction * 100).toFixed(0)}%)
                        </span>
                      )}
                    </span>
                  </div>
                </div>

                {/* Above 100% of the account the position needs margin. Not an
                    error — the account has it — but it should be a decision
                    rather than a surprise noticed after the fill. */}
                {sizing.accountFraction !== null && sizing.accountFraction > 1 && (
                  <p className="text-[10px] text-loss">
                    Costs more than the account holds — needs margin.
                  </p>
                )}

                {sizing.wholeShares === 0 && (
                  <p className="text-[10px] text-loss">
                    Risk budget is smaller than one share&apos;s risk. Widen the
                    account size, raise the risk %, or tighten the stop.
                  </p>
                )}

                {/* The R ladder. Clicking one writes it into Take Profit
                    above, because a plan stores a single target — these are
                    the options, and the field records which was chosen. */}
                <div>
                  <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
                    Take Profit Targets
                  </span>
                  <div className="mt-1 grid grid-cols-3 gap-2">
                    {sizing.targets.map((t) => {
                      const chosen =
                        toNullableNumber(form.takeProfitPrice) !== null &&
                        Math.abs(
                          (toNullableNumber(form.takeProfitPrice) as number) - t.price
                        ) < 0.005;
                      return (
                        <button
                          key={t.r}
                          type="button"
                          onClick={() => patch({ takeProfitPrice: price(t.price) })}
                          disabled={isSaving}
                          aria-pressed={chosen}
                          // The visible label is three separate spans of
                          // numbers, which reads as an unnamed button to a
                          // screen reader. Spelled out here instead.
                          aria-label={`Set take profit to ${price(t.price)} (${t.r}R)`}
                          className={`rounded-lg border px-2 py-1.5 text-left transition-colors disabled:opacity-50 ${
                            chosen
                              ? 'border-win/50 bg-win/15'
                              : 'border-obsidian-border bg-obsidian-bg hover:border-slate-600'
                          }`}
                        >
                          <span
                            className={`block text-[10px] font-semibold ${
                              chosen ? 'text-win' : 'text-obsidian-muted'
                            }`}
                          >
                            {t.r}R
                          </span>
                          <span className="block font-mono text-[11px] text-slate-200">
                            {price(t.price)}
                          </span>
                          {t.profit !== null && (
                            <span className="block font-mono text-[10px] text-obsidian-muted">
                              +{money(t.profit)}
                            </span>
                          )}
                        </button>
                      );
                    })}
                  </div>
                </div>

                {/* What the take profit in the form above is worth.
                    The ladder answers "where is 2R?"; this answers the
                    question you actually arrive with — "I want out at 25,
                    what does that pay?" — which otherwise means eyeballing
                    where 25 falls between two chips and interpolating. */}
                {takeProfitScore !== null && (
                  <div
                    className={`rounded-lg border px-2.5 py-2 ${
                      takeProfitScore.isBackwards
                        ? 'border-loss/40 bg-loss/5'
                        : 'border-obsidian-border bg-obsidian-bg/60'
                    }`}
                  >
                    {takeProfitScore.isBackwards ? (
                      // Named as the typo it is rather than rendered as a
                      // negative R, which would read like a deliberate choice.
                      <p className="text-[10px] leading-relaxed text-loss">
                        Take profit {price(toNullableNumber(form.takeProfitPrice) as number)}{' '}
                        is on the losing side of your entry
                        {form.side === 'BUY'
                          ? ' — for a long it has to sit above it.'
                          : ' — for a short it has to sit below it.'}
                      </p>
                    ) : (
                      <>
                        <div className="flex items-baseline justify-between">
                          <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
                            Your take profit
                          </span>
                          <span className="font-mono text-[11px] text-slate-200">
                            {takeProfitScore.rMultiple.toFixed(2)}R
                            {takeProfitScore.profit !== null && (
                              <span className="ml-1.5 text-win">
                                +{money(takeProfitScore.profit)}
                              </span>
                            )}
                          </span>
                        </div>
                        <p className="mt-0.5 text-[10px] text-obsidian-muted">
                          {money(takeProfitScore.perShare)} per share
                          {takeProfitScore.shares !== null &&
                            ` on ${takeProfitScore.shares} share${
                              takeProfitScore.shares === 1 ? '' : 's'
                            }`}
                          {enteredQty === null &&
                            takeProfitScore.shares !== null &&
                            ' (suggested size)'}
                        </p>
                      </>
                    )}
                  </div>
                )}

                {sizing.wholeShares !== null && sizing.wholeShares > 0 && (
                  <button
                    type="button"
                    onClick={() => patch({ quantity: String(sizing.wholeShares) })}
                    disabled={isSaving}
                    className="w-full rounded-lg border border-obsidian-border bg-obsidian-bg px-3 py-1.5 text-[11px] text-slate-300 hover:border-slate-600 hover:text-slate-100 transition-colors disabled:opacity-50"
                  >
                    Use {sizing.wholeShares} shares as planned quantity
                  </button>
                )}

                {/* What will actually be recorded, which follows the quantity
                    field rather than the suggestion above it. */}
                {plannedRisk !== null && (
                  <p className="text-[10px] text-obsidian-muted">
                    Planning {enteredQty} share{enteredQty === 1 ? '' : 's'} —
                    risking{' '}
                    <span className="text-slate-300">{money(plannedRisk)}</span>
                    {plannedRiskPercent !== null &&
                      ` (${plannedRiskPercent.toFixed(2)}% of account)`}
                    . Saved with the plan.
                  </p>
                )}
              </div>
            )}
          </fieldset>

          {/* The idea. Captured now, before the outcome is known — a thesis
              written after the fact is just the result with reasoning bolted
              on, which is the bias a journal exists to catch. Recording it
              here rather than in the ledger is what makes it verifiable: the
              plan carries a timestamp that predates the fill. */}
          <fieldset className="rounded-lg border border-obsidian-border p-3">
            <legend className="px-1.5 text-[10px] uppercase tracking-wider text-obsidian-muted">
              The Idea
            </legend>

            <label className="block">
              <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
                Strategy
              </span>
              <select
                value={form.strategyId}
                onChange={(e) => patch({ strategyId: e.target.value })}
                disabled={isSaving}
                className={`mt-1 text-xs ${fieldClass}`}
              >
                <option value="">— None —</option>
                {(strategies ?? []).map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                    {s.method ? ` · ${s.method}` : ''}
                  </option>
                ))}
              </select>
              {strategies && strategies.length === 0 && (
                <span className="mt-1 block text-[10px] text-obsidian-muted">
                  No strategies yet — add them in the Strategy Playbook.
                </span>
              )}
            </label>

            <label className="mt-3 block">
              <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
                Why this trade?
              </span>
              <textarea
                value={form.thesis}
                onChange={(e) => patch({ thesis: e.target.value })}
                disabled={isSaving}
                rows={3}
                placeholder="Setup, trigger, and what would prove you wrong."
                className={`mt-1 text-xs resize-y ${fieldClass}`}
              />
            </label>
            <p className="mt-1.5 text-[10px] text-obsidian-muted">
              Written before entry. The post-trade review comes later, in the
              Journal.
            </p>

            {/* The other half of the thesis. "Reclaiming the 50 EMA after
                basing three days" is a sentence; whether the base was
                actually there is a picture, and reviewing the trade later
                without it grades the sentence rather than the decision. */}
            <div className="mt-3">
              <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
                Chart at entry
              </span>
              <div className="mt-1">
                <ChartDropzone value={chart} onChange={setChart} disabled={isSaving} />
              </div>
            </div>
          </fieldset>

          {error && (
            <div className="flex items-start text-xs text-loss">
              <AlertCircle className="h-3.5 w-3.5 mr-1.5 mt-px shrink-0" />
              <span>{error}</span>
            </div>
          )}

          {savedSummary && (
            <div className="flex items-start text-xs text-win">
              <Check className="h-3.5 w-3.5 mr-1.5 mt-px shrink-0" />
              <span>{savedSummary}</span>
            </div>
          )}

          <div className="flex justify-end gap-2 pt-1">
            <button
              type="button"
              onClick={onClose}
              disabled={isSaving}
              className="rounded-lg border border-obsidian-border bg-obsidian-bg px-3.5 py-2 text-xs font-medium text-obsidian-muted hover:text-slate-200 hover:border-slate-600 transition-colors disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isSaving}
              className="inline-flex items-center gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-4 py-2 text-xs font-medium text-amber-300 hover:bg-amber-500/20 disabled:opacity-60 disabled:cursor-not-allowed transition-colors"
            >
              {isSaving ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  Saving…
                </>
              ) : (
                <>
                  <ClipboardList className="h-3.5 w-3.5" />
                  Save Plan
                </>
              )}
            </button>
          </div>
        </form>
      </div>
    </div>,
    document.body
  );
}

export default CreatePlanModal;

'use client';

import React from 'react';
import { AlertTriangle, Check, Clock, Wrench, XCircle } from 'lucide-react';

import type { FlexFailure, IngestResult } from '@/types/api';

/**
 * Tone and title for a finished run, by outcome.
 *
 * Shared rather than duplicated: the toast and the badge's detail modal
 * describe the SAME run, and two copies of this mapping is exactly how one
 * ends up calling a partial run "complete" after the other is edited.
 */
export const SYNC_TONE = {
  success: {
    border: 'border-win-border',
    bg: 'bg-win-glow',
    text: 'text-win',
    Icon: Check,
    title: 'Sync complete',
  },
  partial: {
    border: 'border-amber-500/40',
    bg: 'bg-amber-500/10',
    text: 'text-amber-300',
    Icon: AlertTriangle,
    title: 'Sync incomplete',
  },
  error: {
    border: 'border-loss-border',
    bg: 'bg-loss-glow',
    text: 'text-loss',
    Icon: XCircle,
    title: 'Sync failed',
  },
} as const;

export type SyncOutcome = keyof typeof SYNC_TONE;

/**
 * How each kind of Flex failure should be summed up, and whether waiting is
 * the right response.
 *
 * Keyed by the backend's `category` (services/ibkr_client.FLEX_CODES), so the
 * two cannot drift into telling the user different things.
 */
const FAILURE_COPY: Record<string, { headline: string; advice: string }> = {
  wait: {
    headline: 'IBKR could not produce the report yet',
    advice:
      'Nothing here is misconfigured — this one is on IBKR’s side and resolves without any action.',
  },
  query: {
    headline: 'The Flex query itself needs fixing',
    advice:
      'Waiting will not help. Check the query in Reports → Flex Queries: its id, its account, its period, and that it is exposed to the Flex Web Service.',
  },
  token: {
    headline: 'The Flex credential needs fixing',
    advice:
      'Waiting will not help. The token is expired, invalid, or restricted — reissue it and update IBKR_TOKEN.',
  },
  request: {
    headline: 'IBKR rejected the request itself',
    advice: 'This is a bug on our side rather than something you can configure.',
  },
};

/**
 * Which failures a run hit, and what to do about them.
 *
 * Grouped by category rather than listed flat: two queries refused for the
 * same reason are one instruction, and two refused for different reasons must
 * never be collapsed into one — that collapse is what turned a statement
 * IBKR had not compiled yet into "try again in a few minutes" and cost a day
 * of waiting for something that only clears at a different hour.
 *
 * Falls back to the old flat list when `flex_failures` is absent, which is the
 * window where a browser is running a bundle newer than the API.
 */
const FailurePanel: React.FC<{ result: IngestResult }> = ({ result }) => {
  const failures = result.flex_failures ?? [];

  const grouped = React.useMemo(() => {
    const byCategory = new Map<string, FlexFailure[]>();
    for (const failure of failures) {
      const key = failure.category || 'request';
      if (!byCategory.has(key)) byCategory.set(key, []);
      byCategory.get(key)!.push(failure);
    }
    return [...byCategory.entries()];
  }, [failures]);

  return (
    <div className="mt-3 space-y-2">
      {grouped.length === 0 ? (
        // No reading available: say only what is certain rather than guessing
        // a cause, which is the mistake this whole panel is correcting.
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 px-2.5 py-2">
          <div className="flex items-start gap-1.5">
            <Clock className="mt-px h-3 w-3 shrink-0 text-amber-400" />
            <div className="text-[11px] leading-relaxed text-amber-200/90">
              <span className="font-semibold">
                {result.queries_failed.length} quer
                {result.queries_failed.length === 1 ? 'y' : 'ies'} did not return.
              </span>{' '}
              Some fills may be missing.
            </div>
          </div>
          <FailureList messages={result.queries_failed} />
        </div>
      ) : (
        grouped.map(([category, group]) => {
          const copy = FAILURE_COPY[category] ?? FAILURE_COPY.request;
          const waiting = category === 'wait';
          return (
            <div
              key={category}
              className={`rounded-lg border px-2.5 py-2 ${
                waiting
                  ? 'border-amber-500/30 bg-amber-500/5'
                  : 'border-loss/30 bg-loss/5'
              }`}
            >
              <div className="flex items-start gap-1.5">
                {waiting ? (
                  <Clock className="mt-px h-3 w-3 shrink-0 text-amber-400" />
                ) : (
                  <Wrench className="mt-px h-3 w-3 shrink-0 text-loss" />
                )}
                <div
                  className={`text-[11px] leading-relaxed ${
                    waiting ? 'text-amber-200/90' : 'text-loss'
                  }`}
                >
                  <span className="font-semibold">
                    {group.length} quer{group.length === 1 ? 'y' : 'ies'}:{' '}
                    {copy.headline}.
                  </span>{' '}
                  {copy.advice}
                  {/* The per-code detail, which is what makes this specific
                      rather than a category label. */}
                  {group[0].guidance && (
                    <span className="mt-1 block text-amber-200/70">
                      {group[0].guidance}
                    </span>
                  )}
                </div>
              </div>
              <FailureList messages={group.map((f) => f.message)} />
            </div>
          );
        })
      )}
    </div>
  );
};

/** IBKR's own wording, kept verbatim so our reading can be checked against it. */
const FailureList: React.FC<{ messages: string[] }> = ({ messages }) => (
  <ul className="mt-1.5 space-y-0.5 pl-4.5">
    {messages.map((message) => (
      <li
        key={message}
        className="break-words font-mono text-[10px] text-amber-200/60"
      >
        {message}
      </li>
    ))}
  </ul>
);

/**
 * Everything one finished run did, as a block of content.
 *
 * Extracted from SyncResultToast so the badge's detail modal can render the
 * same figures from a PERSISTED run. That mattered more than avoiding
 * duplication: `sync_runs.result` has always carried the full payload
 * precisely so "the toast can be rendered from a run the tab did not
 * perform", but nothing ever did, so a scheduled run that finished overnight
 * left its detail sitting in the query cache with no way to see it.
 *
 * Pure presentation, no hooks -- both callers supply the run.
 */
export const SyncRunDetail: React.FC<{
  result: IngestResult | null;
  /** Shown in place of the figures when the run produced no payload. */
  summary: string;
}> = ({ result, summary }) => {
  // Only the figures that carry information -- which turned out to include
  // `executions_parsed`, dropped here originally as "describing the staging
  // table rather than the ledger". On a re-sync where nothing is new, every
  // ledger-side counter is legitimately zero and the staging figures are the
  // only evidence the Flex query returned anything at all.
  //
  // "Already in the ledger" sums both duplicate counters on purpose. A fill
  // stopped at staging and one stopped at the trades insert are the same fact
  // to the reader: the broker re-sent it, and the journal already had it.
  // Reporting only the second reads as 0 whenever the first caught everything.
  const rows: { label: string; value: number; hint?: string }[] = result
    ? [
        {
          // Without this the panel reads 0 across the board on any re-sync,
          // which is indistinguishable from IBKR returning nothing at all --
          // and telling those two apart is the whole reason to look.
          label: 'Executions IBKR returned',
          value: result.executions_parsed,
          hint: 'Rows in the Flex report. Zero means the query came back empty.',
        },
        { label: 'New fills imported', value: result.trades_created },
        {
          label: 'Already in the ledger',
          value: result.staged_duplicates + result.trades_duplicates,
          hint: 'Recognised by broker id and skipped — this is idempotency working.',
        },
        {
          label: 'Suppressed, skipped',
          value: result.suppressed_skipped ?? 0,
          hint: 'Fills you deleted. IBKR re-sent them; the tombstone held.',
        },
        {
          label: 'Trade plans attached',
          value: result.plans_attached ?? 0,
          hint: 'A plan you wrote beforehand met the fill it was waiting for.',
        },
        {
          label: 'Non-tradeable rows',
          value: result.skipped_non_tradeable,
          hint: 'Currency conversions and similar, which are not positions.',
        },
        { label: 'Round trips matched', value: result.positions_matched },
      ]
    : [];

  return (
    <div className="px-4 py-3">
      {result ? (
        <>
          <dl className="space-y-1.5">
            {rows.map((row) => (
              <div key={row.label} className="flex items-baseline justify-between gap-3">
                <dt
                  className="text-[11px] text-obsidian-muted"
                  title={row.hint}
                >
                  {row.label}
                </dt>
                <dd
                  className={`font-mono text-xs ${
                    row.value > 0 ? 'text-slate-200' : 'text-obsidian-muted'
                  }`}
                >
                  {row.value}
                </dd>
              </div>
            ))}
          </dl>

          {result.symbols_touched.length > 0 && (
            <p className="mt-2 text-[10px] text-obsidian-muted">
              Tickers rebuilt: {result.symbols_touched.join(', ')}
            </p>
          )}

          {/* Rare, and never something to discover later from a changed
              headline. A fill dated earlier than ones already stored
              re-partitions FIFO for that ticker, so round trips closed by
              an earlier run can stop existing — taking their P&L out of
              every statistic, and their review with them. */}
          {(result.positions_removed ?? 0) > 0 && (
            <p className="mt-3 rounded-lg border border-loss/30 bg-loss/5 px-2.5 py-2 text-[11px] leading-relaxed text-loss">
              <span className="font-semibold">
                {result.positions_removed} closed round trip
                {result.positions_removed === 1 ? '' : 's'} no longer
                match{result.positions_removed === 1 ? 'es' : ''} and{' '}
                {result.positions_removed === 1 ? 'was' : 'were'} removed.
              </span>{' '}
              A fill arrived dated earlier than ones already recorded, which
              re-pairs that ticker&apos;s trades. Your P&amp;L and trade
              count have changed accordingly.
              {(result.reviews_discarded ?? 0) > 0 && (
                <>
                  {' '}
                  <span className="font-semibold">
                    {result.reviews_discarded} review
                    {result.reviews_discarded === 1 ? '' : 's'}
                  </span>{' '}
                  could not be carried over.
                </>
              )}
            </p>
          )}

          {/* An earlier sync imported these fills and died before building
              round trips from them, so they sat in the ledger as exposure
              nothing could see. This run repaired it — which means P&L and
              trade count just moved for a reason none of the counters
              above explain. */}
          {(result.symbols_recovered?.length ?? 0) > 0 && (
            <p className="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/5 px-2.5 py-2 text-[11px] leading-relaxed text-amber-200/90">
              <span className="font-semibold">
                Recovered {result.symbols_recovered!.length} ticker
                {result.symbols_recovered!.length === 1 ? '' : 's'} from an
                interrupted sync.
              </span>{' '}
              {result.symbols_recovered!.join(', ')} had fills imported but
              never matched into round trips. They are counted now, so your
              totals have changed.
            </p>
          )}

          {/* A STANDING condition, not a per-sync event — deliberately
              distinct in wording and styling from "Recovered" above. That
              banner reports fills that WERE imported and just got matched
              late; this one reports fills that were never imported at
              all, and says so on every sync until a human resolves it.
              Neither the wrench icon nor "the repair-fill modal" phrase
              is decorative: this is the same fix path TradeLedger already
              points to for a hand-typed correction. */}
          {(result.stranded_fills ?? 0) > 0 && (
            <p className="mt-3 rounded-lg border border-slate-600/40 bg-slate-800/40 px-2.5 py-2 text-[11px] leading-relaxed text-slate-300">
              <span className="inline-flex items-center gap-1 font-semibold text-slate-200">
                <Wrench className="h-3 w-3 shrink-0" />
                {result.stranded_fills} fill
                {result.stranded_fills === 1 ? '' : 's'} still cannot be
                imported.
              </span>{' '}
              IBKR sent{' '}
              {result.stranded_fills === 1 ? 'it' : 'them'} with no price,
              no execution time, or another data issue, so{' '}
              {result.stranded_fills === 1 ? 'it is' : 'they are'} not in
              the ledger and not counted in any figure above.{' '}
              {(result.stranded_symbols?.length ?? 0) > 0 && (
                <>Affects {result.stranded_symbols!.join(', ')}. </>
              )}
              Add {result.stranded_fills === 1 ? 'it' : 'them'} by hand
              from the repair-fill modal, or wait for IBKR to resend a
              corrected statement — this will keep showing up until then.
            </p>
          )}

          {/* Called out rather than left as a number in the list: this is
              the one outcome that changed a trade's recorded intent, and
              it is worth checking the sync matched the plan you meant. */}
          {(result.plans_attached ?? 0) > 0 && (
            <p className="mt-2 rounded-lg border border-amber-500/25 bg-amber-500/5 px-2.5 py-2 text-[11px] leading-relaxed text-amber-200/90">
              <span className="font-semibold">
                {result.plans_attached} trade plan
                {result.plans_attached === 1 ? '' : 's'} attached.
              </span>{' '}
              Your planned stop and target are now on the fill. Check it in
              the Journal if more than one plan could have matched.
            </p>
          )}
        </>
      ) : (
        <p className="text-[11px] text-loss">{summary}</p>
      )}

      {/* The actionable case, and the one that has to say which KIND of
          failure this is. "Try again shortly" is right for a statement
          IBKR has not compiled yet and actively wrong for an expired
          token, and this panel used to give the same advice for both. */}
      {result && result.queries_failed.length > 0 && (
        <FailurePanel result={result} />
      )}
    </div>

  );
};

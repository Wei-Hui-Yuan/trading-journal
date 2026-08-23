'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query';

import {
  annotateTrade,
  attachPlan,
  cancelPlan,
  deletePlanChart,
  getPlanChart,
  uploadPlanChart,
  createDiscipline,
  createManualTrade,
  createPlan,
  createStrategy,
  createTimeframe,
  deleteDiscipline,
  deleteTimeframe,
  getTimeframes,
  updateTimeframe,
  deletePosition,
  deleteStrategy,
  deleteTrade,
  detachPlan,
  dismissPosition,
  getPlans,
  updatePlan,
  getAdvancedMetrics,
  getDashboardAnalytics,
  getDisciplines,
  getPendingPositions,
  getPositionDeleteImpact,
  getPositionFills,
  getRoundTrips,
  getSettings,
  getStrategies,
  getSuppressedExecutions,
  getSyncStatus,
  getTrades,
  httpStatusOf,
  ingestIBKR,
  updateExecution,
  updatePositionReview,
  unsuppressExecution,
  updateSettings,
  updateStrategy,
} from '@/lib/api';
import type {
  AdvancedMetrics,
  AppSettings,
  AppSettingsPayload,
  DashboardStats,
  Discipline,
  DisciplineCreatePayload,
  ExecutionUpdatePayload,
  ExecutionUpdateResult,
  IngestResult,
  IngestStarted,
  LastSyncState,
  PositionDeleteImpact,
  PositionDeleteResult,
  SyncStatus,
  ManualTradePayload,
  ManualTradeResult,
  PlanAttachResult,
  PlanDetachResult,
  PlanStatus,
  TradePlan,
  TradePlanPayload,
  TradePlanUpdatePayload,
  Position,
  PositionFill,
  PositionReviewPayload,
  RoundTrip,
  Strategy,
  SuppressedExecution,
  StrategyCreatePayload,
  StrategyDeleteResult,
  StrategyUpdatePayload,
  Trade,
  TradeDeleteResult,
  TradeAnnotationPayload,
  TimeframePreset,
  TimeframePresetPayload,
  TimeframeSelection,
  UnsuppressResult,
} from '@/types/api';

/**
 * How many CLOSED round trips one page carries.
 *
 * Large enough that the common case — opening the journal and scanning recent
 * trades — is one request and no "load more", and small enough that the page
 * stays cheap once the ledger has years in it. Open exposure is not counted
 * against it; that always arrives whole on the first page.
 */
export const ROUND_TRIP_PAGE_SIZE = 50;

/** What the journal is currently narrowed to. Sent to the server, not applied
 * here — see useRoundTrips. */
export interface RoundTripFilters {
  /** Undefined means both halves. */
  kind?: 'open' | 'closed';
  /** A strategy id, 'unassigned', or undefined for any. */
  strategy?: string;
  /** Substring match on the symbol. Empty means no search. */
  search: string;
}

/**
 * Query keys, centralized so a hook and its invalidator can never drift apart.
 */
export const queryKeys = {
  pendingPositions: ['positions', 'pending'] as const,
  trades: ['trades'] as const,
  // Prefix. Every filter combination is its own cache entry beneath it, so the
  // dozen or so mutations that invalidate this one key keep refreshing all of
  // them — which they must, since a review written on one filter changes what
  // the others show.
  roundTrips: ['roundTrips'] as const,
  roundTripsFor: (filters: RoundTripFilters) =>
    [
      'roundTrips',
      filters.kind ?? 'all',
      filters.strategy ?? 'all',
      // Normalised, so `aapl` and `AAPL ` do not open two entries holding the
      // same rows — the server upper-cases and trims before matching.
      filters.search.trim().toUpperCase(),
    ] as const,
  positionFills: (id: string) => ['positions', id, 'fills'] as const,
  strategies: ['strategies'] as const,
  disciplines: ['disciplines'] as const,
  // Prefix. Every window is its own cache entry beneath it, so invalidating
  // this one key refreshes all of them — which is what the sync, review and
  // delete mutations already do, and must keep doing.
  dashboardStats: ['dashboardStats'] as const,
  dashboardStatsFor: (selection?: TimeframeSelection) =>
    [
      'dashboardStats',
      selection?.kind === 'custom'
        ? // Keyed by the dates, not the preset id: renaming a saved window does
          // not change what it selects, and two presets covering the same range
          // can honestly share a cache entry.
          `${selection.start_date}..${selection.end_date}`
        : (selection?.preset ?? '1Y'),
    ] as const,
  timeframes: ['timeframes'] as const,
  // Prefix, exactly like dashboardStats above. Every window is its own entry
  // beneath it, so the mutations that invalidate this one key still refresh
  // all of them.
  advancedMetrics: ['advancedMetrics'] as const,
  advancedMetricsFor: (selection?: TimeframeSelection) =>
    [
      'advancedMetrics',
      selection?.kind === 'custom'
        ? // Keyed by the dates rather than the preset id, for the same reason
          // dashboardStatsFor is: renaming a saved window does not change what
          // it selects, and two presets covering one range can share an entry.
          `${selection.start_date}..${selection.end_date}`
        : (selection?.preset ?? '1Y'),
    ] as const,
  settings: ['settings'] as const,
  // Written by the sync mutation, read by the header badge. Not a fetched
  // resource — the cache is being used as the one place both can see.
  lastSync: ['lastSync'] as const,
  // The DURABLE record, from the server. Distinct from `lastSync` above,
  // which only ever knows about a sync this tab performed and therefore
  // cannot see a scheduled run at all.
  syncStatus: ['syncStatus'] as const,
  suppressed: ['suppressedExecutions'] as const,
  // Prefix, so invalidating plans clears every status filter at once — a
  // cancelled plan has to leave the OPEN list and appear in the ALL list, and
  // those are two different cache entries.
  plansRoot: ['plans'] as const,
  plans: (status: string) => ['plans', status] as const,
  // Kept OUTSIDE plansRoot on purpose. Editing a plan's levels invalidates
  // plansRoot constantly, and the screenshot has not changed — nesting this
  // under it would re-download the image on every unrelated edit.
  planChart: (planId: string) => ['planChart', planId] as const,
};

/** Positions awaiting review — the Trade Inbox queue. */
export function usePendingPositions() {
  return useQuery<Position[]>({
    queryKey: queryKeys.pendingPositions,
    queryFn: getPendingPositions,
  });
}

/**
 * The executions behind one position, for the drill-down.
 *
 * `enabled` gates the request on the row actually being expanded, so opening
 * the inbox does not fan out a fetch per position. Fills are immutable once
 * matched, hence the long staleTime.
 */
export function usePositionFills(positionId: string, enabled: boolean) {
  return useQuery<PositionFill[]>({
    queryKey: queryKeys.positionFills(positionId),
    queryFn: () => getPositionFills(positionId),
    enabled,
    staleTime: 5 * 60_000,
  });
}

/** Every execution, open or closed — the master list. */
export function useTrades() {
  return useQuery<Trade[]>({
    queryKey: queryKeys.trades,
    queryFn: () => getTrades(),
  });
}

/**
 * The journal: one row per trade idea, open or closed.
 *
 * Paged, and filtered by the SERVER. Both matter and for the same reason: the
 * ledger is meant to accumulate for years, and a filter applied to the rows
 * already fetched narrows a page rather than the journal. A search for a
 * symbol has to be able to find it wherever it sits in the history, not only
 * where it happens to have been loaded.
 *
 * Open exposure arrives on the first page only, so `flat` below can simply
 * concatenate. Paging applies to closed round trips.
 */
export function useRoundTrips(filters: RoundTripFilters) {
  const query = useInfiniteQuery({
    queryKey: queryKeys.roundTripsFor(filters),
    queryFn: ({ pageParam }) =>
      getRoundTrips({
        kind: filters.kind,
        strategy: filters.strategy,
        search: filters.search.trim() || undefined,
        limit: ROUND_TRIP_PAGE_SIZE,
        offset: pageParam,
      }),
    // Every filter and every debounced search term is its own cache key, so
    // without this each one starts empty and `isLoading` flips true — which
    // unmounts the whole ledger, including the search box being typed into.
    // The caret went with it, so the second character of a ticker landed
    // nowhere. Holding the previous rows keeps the controls mounted and makes
    // filtering a swap rather than a blank screen.
    //
    // Same guard, and same reason, as useDashboardStats below.
    placeholderData: (previous) => previous,
    initialPageParam: 0,
    getNextPageParam: (lastPage, _all, lastOffset) => {
      // Counted over CLOSED rows only. The first page also carries open
      // exposure, so measuring the whole page against the size would read a
      // full page as partial whenever anything is open, and stop paging one
      // page early — hiding closed history behind a button that had already
      // decided there was no more.
      const closed = lastPage.filter((rt) => rt.kind === 'closed').length;
      return closed < ROUND_TRIP_PAGE_SIZE
        ? undefined
        : lastOffset + ROUND_TRIP_PAGE_SIZE;
    },
  });

  return {
    ...query,
    /** Every page so far, in order, as one list. */
    flat: useMemo(
      () => (query.data?.pages ?? []).flat(),
      [query.data]
    ),
  };
}

/**
 * How many positions are currently open, independent of what the journal is
 * filtered to.
 *
 * Its own request rather than a count over the loaded rows, because those are
 * now server-filtered: under the CLOSED filter no open row comes back at all,
 * and counting them would report zero open positions while several were live.
 * The badge has to mean "how many are open", not "how many are open and also
 * match what you are looking at".
 *
 * Cheap to ask for. Open exposure is bounded by how many tickers can be held
 * at once, never returns more than a handful of rows, and shares the journal's
 * cache prefix so every mutation already refreshes it.
 */
export function useOpenRoundTripCount() {
  const { data } = useQuery<RoundTrip[]>({
    queryKey: [...queryKeys.roundTrips, 'openCount'] as const,
    queryFn: () => getRoundTrips({ kind: 'open' }),
  });
  return data?.length ?? 0;
}

/** Attach a strategy, thesis or plan to a round trip's opening execution. */
export function useAnnotateTrade() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: TradeAnnotationPayload }) =>
      annotateTrade(id, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.trades });
      // The journal reads the plan off this execution, and analytics scores R
      // from it -- both go stale the moment a stop changes.
      queryClient.invalidateQueries({ queryKey: queryKeys.roundTrips });
      queryClient.invalidateQueries({ queryKey: queryKeys.advancedMetrics });
    },
  });
}

/**
 * Account size and default risk, backing the position-size calculator.
 *
 * `retry: false` because the calculator degrades to hand-typed values when
 * this fails — spending three retries before the form becomes usable is worse
 * than showing the fallback immediately.
 */
export function useSettings() {
  return useQuery<AppSettings>({
    queryKey: queryKeys.settings,
    queryFn: getSettings,
    staleTime: 5 * 60_000,
    retry: false,
  });
}

/** Persist account size / default risk so the next trade opens with them. */
export function useUpdateSettings() {
  const queryClient = useQueryClient();
  return useMutation<AppSettings, Error, AppSettingsPayload>({
    mutationFn: updateSettings,
    // Written straight into the cache rather than invalidated: the modal reads
    // these while the user is still typing into the same form, and a refetch
    // round trip would briefly restore the old account size under the cursor.
    onSuccess: (saved) => queryClient.setQueryData(queryKeys.settings, saved),
  });
}

/** Strategies for the review dropdown. Rarely changes, so cache it longer. */
export function useStrategies() {
  return useQuery<Strategy[]>({
    queryKey: queryKeys.strategies,
    queryFn: getStrategies,
    staleTime: 5 * 60_000,
  });
}

/**
 * Core stats, heatmap and equity curve for one timeframe window.
 *
 * The window is part of the query key, so switching pills is a cached lookup
 * after the first visit and flipping back and forth costs nothing. The key
 * stays PREFIXED with 'dashboardStats', which is what keeps the seven existing
 * `invalidateQueries({ queryKey: queryKeys.dashboardStats })` calls correct:
 * React Query matches keys by prefix, so a sync still refreshes every window
 * that has been looked at rather than only the one on screen.
 */
export function useDashboardStats(selection?: TimeframeSelection) {
  return useQuery<DashboardStats>({
    queryKey: queryKeys.dashboardStatsFor(selection),
    queryFn: () => getDashboardAnalytics(selection),
    // A window that has already been fetched is worth keeping while another is
    // loading, so switching pills does not blank the whole dashboard.
    placeholderData: (previous) => previous,
  });
}

/** Saved custom windows for the timeframe toolbar. */
export function useTimeframes() {
  return useQuery<TimeframePreset[]>({
    queryKey: queryKeys.timeframes,
    queryFn: getTimeframes,
    // Presets change only when the user edits them, and every mutation below
    // invalidates. No reason to refetch on every dashboard mount.
    staleTime: 5 * 60_000,
  });
}

/**
 * Add, edit or remove a saved window.
 *
 * All three invalidate the dashboard as well as the preset list. Editing a
 * preset's dates changes what the pill means, and the payload already on
 * screen was computed for the old range — leaving it would show figures
 * labelled with a window they were not computed over.
 */
function useTimeframeMutation<TVariables>(
  mutationFn: (variables: TVariables) => Promise<TimeframePreset>
) {
  const queryClient = useQueryClient();

  return useMutation<TimeframePreset, Error, TVariables>({
    mutationFn,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.timeframes });
      queryClient.invalidateQueries({ queryKey: queryKeys.dashboardStats });
    },
  });
}

export function useCreateTimeframe() {
  return useTimeframeMutation<TimeframePresetPayload>(createTimeframe);
}

export function useUpdateTimeframe() {
  return useTimeframeMutation<{ id: string; payload: TimeframePresetPayload }>(
    ({ id, payload }) => updateTimeframe(id, payload)
  );
}

export function useDeleteTimeframe() {
  return useTimeframeMutation<string>(deleteTimeframe);
}

/** R-multiple, slippage and expectancy metrics for the Analytics tab. */
export function useAdvancedMetrics(selection?: TimeframeSelection) {
  return useQuery<AdvancedMetrics>({
    queryKey: queryKeys.advancedMetricsFor(selection),
    queryFn: () => getAdvancedMetrics(selection),
    // A window already fetched is worth keeping on screen while another loads,
    // so switching pills does not blank the page. Same guard, same reason, as
    // useDashboardStats.
    placeholderData: (previous) => previous,
  });
}

/**
 * A stored run's result, made safe to render.
 *
 * `sync_runs.result` is a snapshot of whatever IngestResult looked like when the
 * row was written, so a run recorded before a field existed simply lacks it. The
 * toast reads sixteen fields and indexes into three arrays, so handing it a
 * partial directly would throw on exactly the historical rows this exists to
 * surface. Stored values win; the defaults only fill genuine absences.
 */
function completeResult(
  stored: Partial<IngestResult> | null | undefined
): IngestResult | null {
  if (!stored) return null;
  return {
    executions_parsed: 0,
    staged_new: 0,
    staged_duplicates: 0,
    trades_created: 0,
    trades_duplicates: 0,
    positions_matched: 0,
    symbols_touched: [],
    skipped_non_tradeable: 0,
    queries_failed: [],
    ...stored,
  };
}

/**
 * The finished run the header is describing -- this tab's, or the server's.
 *
 * Extracted from SyncStatusBadge so the badge and the detail modal it opens
 * cannot disagree about WHICH run they are talking about. A badge reading
 * "12:44" that opens a panel describing last night's cron run is worse than
 * no panel, and two copies of this rule is exactly how that happens.
 *
 * Returns null while a run is in flight or nothing has ever run: both are
 * states the badge renders itself and neither has a result to show.
 */
export function useLatestSyncRun(): {
  outcome: 'success' | 'partial' | 'error';
  at: string;
  /** Completed from the stored partial, so it is always safe to render. */
  result: IngestResult | null;
  summary: string;
  /** Only this tab's own runs carry an HTTP status. */
  status: number | null;
  /** 'cron' | 'manual' on a persisted run; null when read from memory. */
  trigger: string | null;
  fromMemory: boolean;
} | null {
  const lastSync = useLastSync();
  const { data } = useSyncStatus();
  const persisted = data?.latest ?? null;

  const running = persisted?.outcome === 'running' && !data?.latest_looks_abandoned;
  if (running) return null;
  if (!lastSync && !persisted) return null;

  // Prefer whichever actually happened last. Normally that is the in-memory
  // one during a session where you pressed the button, and the persisted one
  // on a fresh page load or after the schedule ran.
  const useMemory =
    lastSync !== undefined &&
    (persisted === null ||
      new Date(lastSync.at).getTime() >= new Date(persisted.started_at).getTime());

  if (useMemory) {
    return {
      outcome: lastSync!.outcome,
      at: lastSync!.at,
      result: lastSync!.result,
      summary: lastSync!.summary,
      status: lastSync!.status,
      trigger: null,
      fromMemory: true,
    };
  }

  // A persisted run can still be 'running' here only when it looks abandoned,
  // which the badge reports as a failure rather than as work in progress.
  const outcome =
    persisted!.outcome === 'success' || persisted!.outcome === 'partial'
      ? persisted!.outcome
      : 'error';

  return {
    outcome,
    at: persisted!.started_at,
    result: completeResult(persisted!.result),
    summary:
      persisted!.error ??
      (persisted!.trades_created > 0
        ? `${persisted!.trades_created} new`
        : `${persisted!.executions_parsed} returned`),
    status: null,
    trigger: persisted!.trigger,
    fromMemory: false,
  };
}

/**
 * React to a sync that has finished: refresh what it changed, and report it.
 *
 * Shared by two callers that used to be one. A browser is now handed a 202 and
 * the outcome arrives later through the polled `sync_runs` row, so this runs
 * from the watcher — but the endpoint still answers synchronously when it could
 * not record a slot row, and that path lands here directly. Both have to behave
 * identically or the fallback would quietly stop invalidating.
 */
function applyFinishedSync(
  queryClient: ReturnType<typeof useQueryClient>,
  result: IngestResult
) {
  // Everything below is derived from the ledger, and the common sync
      // changes none of it. IBKR's rolling window re-reports fills already
      // imported, so the ordinary run -- and every scheduled one on a day
      // without trading -- promotes nothing and matches nothing, while still
      // refetching the app's most expensive queries. `roundTrips` is an
      // infinite query, so invalidating it refetches EVERY loaded page in
      // sequence; `dashboardStats` and `advancedMetrics` are prefixes over one
      // entry per timeframe window visited.
      //
      // Each clause is a distinct way the ledger can move, listed separately
      // rather than collapsed because the reasoning differs:
      //
      //   symbols_touched      -- positions, fills and legs rebuilt for these
      //                           tickers. Covers new fills AND recovered ones.
      //   trades_created       -- new rows in the ledger itself.
      //   plans_attached       -- a plan bound to a fill; implied by the above
      //                           today, kept because that is an implication
      //                           rather than a guarantee.
      //   positions_removed    -- a backdated fill re-partitioned the FIFO
      //                           queue and a round trip stopped existing.
      //   broker figures       -- the one that is NOT implied by any of them:
      //                           refreshed cost basis moves open exposure via
      //                           _acquisition_premium in list_round_trips
      //                           with every counter above still zero.
      //
      // Absent `broker_figures_refreshed` means an API older than the field,
      // and the honest reading of "I do not know" is "assume it did" -- which
      // is the previous behaviour, so a browser deployed ahead of the API
      // stays correct rather than quietly skipping a refresh.
      const ledgerMayHaveChanged =
        result.symbols_touched.length > 0 ||
        result.trades_created > 0 ||
        (result.plans_attached ?? 0) > 0 ||
        (result.positions_removed ?? 0) > 0 ||
        result.broker_figures_refreshed !== false;

      if (ledgerMayHaveChanged) {
        queryClient.invalidateQueries({ queryKey: queryKeys.pendingPositions });
        queryClient.invalidateQueries({ queryKey: queryKeys.dashboardStats });
        // A sync promotes fills into trades and re-runs FIFO matching, so the
        // ledger and every analytics card built on it go stale the same way an
        // annotation or a manual entry already does.
        queryClient.invalidateQueries({ queryKey: queryKeys.trades });
        queryClient.invalidateQueries({ queryKey: queryKeys.roundTrips });
        queryClient.invalidateQueries({ queryKey: queryKeys.advancedMetrics });
      }

      // A run where some Flex queries did not return is NOT a success. IBKR
      // rate-limits report generation per token, and its cooldown outlasts a
      // request, so a partial run reports fewer fills than exist — reporting
      // it as green is how "no new trades" comes to mean "IBKR refused us".
      const partial = result.queries_failed.length > 0;
      queryClient.setQueryData<LastSyncState>(queryKeys.lastSync, {
        at: new Date().toISOString(),
        outcome: partial ? 'partial' : 'success',
        status: 200,
        summary: partial
          ? `${result.queries_failed.length} quer${
              result.queries_failed.length === 1 ? 'y' : 'ies'
            } unavailable`
          : result.trades_created > 0
            ? `${result.trades_created} new`
            : result.staged_duplicates > 0
              ? 'up to date'
              : 'no fills',
        result,
        acknowledged: false,
      });
}

/**
 * Start a broker sync, without waiting for IBKR to finish compiling it.
 *
 * The server answers a browser with 202 and a run id: the Flex handshake takes
 * 15 to 240 seconds, and holding the button hostage for that was the worst
 * interaction in the app. The outcome arrives through `useSyncRunWatcher` below,
 * which follows the recorded row.
 *
 * Invalidation still lives out here rather than in the button so every caller
 * gets a correct refresh — it has just moved to where the run actually ends.
 */
export function useSyncBroker() {
  const queryClient = useQueryClient();

  return useMutation<IngestStarted, Error, void>({
    mutationFn: ingestIBKR,
    onSuccess: (started) => {
      // Always, and this is also what STARTS the poll: refetching syncStatus is
      // how the new `running` row is first observed, and `useSyncStatus` only
      // arms its timer once it has seen one.
      queryClient.invalidateQueries({ queryKey: queryKeys.syncStatus });

      // A handed-off run has changed nothing yet, so there is nothing to
      // invalidate and nothing to report. Deliberately no `lastSync` write
      // either: the badge prefers whichever source is newer, and leaving this
      // one alone lets it read the server's `running` row instead of a local
      // placeholder that would have to be kept in step with it.
      if (started.kind === 'started') return;

      // The synchronous fallback -- the server could not record a slot row and
      // ran the ingest inline, so the outcome is already in hand.
      applyFinishedSync(queryClient, started.result);
    },
    onError: (error) => {
      // The server records failed runs too, so the badge has something new to
      // read even though nothing was imported.
      queryClient.invalidateQueries({ queryKey: queryKeys.syncStatus });
      queryClient.setQueryData<LastSyncState>(queryKeys.lastSync, {
        at: new Date().toISOString(),
        outcome: 'error',
        status: httpStatusOf(error),
        summary: error.message,
        result: null,
        acknowledged: false,
      });
    },
  });
}

/** How often to ask about a run that is in flight. */
const SYNC_POLL_MS = 3_000;

/**
 * The last sync anyone performed, from the server rather than this tab.
 *
 * This is what makes a scheduled run visible: `useLastSync` below can only
 * report syncs THIS browser session ran, so before this a nightly job that
 * failed on an expired token produced the same silence as a quiet market.
 *
 * Polled ONLY while a run is actually in flight, which is the whole mechanism
 * behind the sync button no longer blocking. A browser is handed a 202 and this
 * query follows the row until it reaches a terminal outcome, then stops. When
 * nothing is running there is no timer at all — a daily schedule does not
 * warrant one on every mounted page, and mount plus post-sync invalidation
 * already covers every moment the value visibly changes.
 *
 * `latest_looks_abandoned` is what stops the timer running forever. The server
 * only reaps stranded runs when a sync is STARTED, since a GET has no business
 * writing rows, so a run whose worker was recycled stays `running` in the table
 * until someone syncs again. Polling that would be an eternal 3s timer against
 * a row nobody will ever finish.
 */
export function useSyncStatus() {
  return useQuery<SyncStatus, Error>({
    queryKey: queryKeys.syncStatus,
    queryFn: getSyncStatus,
    refetchInterval: (query) => {
      const status = query.state.data;
      if (!status?.latest) return false;
      if (status.latest.outcome !== 'running') return false;
      return status.latest_looks_abandoned ? false : SYNC_POLL_MS;
    },
    // A run continues on the server whether or not this tab is in front, and
    // coming back to a stale "syncing" badge would be exactly the confusion
    // this endpoint exists to remove.
    refetchIntervalInBackground: true,
  });
}

/**
 * Whether a sync is running right now, anywhere.
 *
 * Reads the server's record rather than this tab's mutation state, so a run
 * started in another tab — or by the scheduler at 9pm — disables the button
 * here too. A per-tab `isPending` could not see either, and two tabs each
 * starting a sync is precisely what the 409 exists to refuse.
 */
export function useSyncInFlight(): boolean {
  const { data } = useSyncStatus();
  return (
    data?.latest?.outcome === 'running' && !data.latest_looks_abandoned
  );
}

/**
 * Notice when an in-flight sync finishes, and do what its mutation used to.
 *
 * MOUNT THIS EXACTLY ONCE. It performs side effects — cache invalidation and
 * the toast — so a second copy would double them. `Header` is the right home:
 * it renders on every page and already owns the badge that reads this.
 *
 * Only fires on a run it watched go from `running` to finished. That distinction
 * is the whole correctness of this hook: on first mount `latest` is almost
 * always some already-terminal run from yesterday, and acting on that would
 * invalidate every query and pop a toast for a sync nobody just performed. So it
 * arms only after seeing `running`, which means it equally catches a run started
 * by the scheduler or in another tab — those are worth reacting to for exactly
 * the same reasons this tab's own run is.
 *
 * Keying on the row id would not work: the id does not change when the run
 * finishes, only the outcome does.
 */
export function useSyncRunWatcher() {
  const queryClient = useQueryClient();
  const { data } = useSyncStatus();
  const wasRunning = useRef(false);

  const latest = data?.latest ?? null;
  const abandoned = data?.latest_looks_abandoned ?? false;

  useEffect(() => {
    if (!latest) return;

    if (latest.outcome === 'running' && !abandoned) {
      wasRunning.current = true;
      return;
    }

    if (!wasRunning.current) return;
    wasRunning.current = false;

    if (abandoned) {
      // The worker went away mid-run. Say so rather than reporting a failure
      // the ingest never produced -- and rather than leaving the badge spinning.
      queryClient.setQueryData<LastSyncState>(queryKeys.lastSync, {
        at: latest.started_at,
        outcome: 'error',
        status: null,
        summary: 'sync stopped reporting',
        result: null,
        acknowledged: false,
      });
      return;
    }

    if (latest.outcome === 'error') {
      queryClient.setQueryData<LastSyncState>(queryKeys.lastSync, {
        at: latest.finished_at ?? latest.started_at,
        outcome: 'error',
        status: null,
        summary: latest.error ?? 'The sync failed.',
        result: null,
        acknowledged: false,
      });
      return;
    }

    const result = completeResult(latest.result);
    if (!result) {
      // Finished, but with no stored payload to report. Nothing to invalidate
      // on either -- without the result there is no way to tell whether the
      // ledger moved, and guessing in the direction of "it did" is the safe one.
      queryClient.invalidateQueries({ queryKey: queryKeys.roundTrips });
      queryClient.invalidateQueries({ queryKey: queryKeys.dashboardStats });
      queryClient.invalidateQueries({ queryKey: queryKeys.advancedMetrics });
      return;
    }

    applyFinishedSync(queryClient, result);
  }, [latest, abandoned, queryClient]);
}

/**
 * The last sync attempt, or undefined if none has run this session.
 *
 * Read-only view of a cache entry the sync mutation writes. Deliberately not
 * persisted: after a reload the honest answer is "not since you opened this",
 * and a remembered timestamp from yesterday would imply a freshness the app
 * cannot vouch for.
 */
export function useLastSync(): LastSyncState | undefined {
  const queryClient = useQueryClient();
  // Subscribes to the cache key so the badge re-renders when a sync lands.
  const { data } = useQuery<LastSyncState | null>({
    queryKey: queryKeys.lastSync,
    // Never fetched; the mutation is the only writer.
    queryFn: () => queryClient.getQueryData<LastSyncState>(queryKeys.lastSync) ?? null,
    staleTime: Infinity,
    gcTime: Infinity,
  });
  return data ?? undefined;
}

/**
 * Hand-log an execution.
 *
 * The backend re-runs FIFO matching on save, so a closing fill can produce a
 * new position immediately. The inbox queue, the dashboard, the ledger and
 * every analytics card built on the round trips are all invalidated so none
 * of them still shows pre-save data without a reload.
 */
export function useCreateManualTrade() {
  const queryClient = useQueryClient();

  return useMutation<ManualTradeResult, Error, ManualTradePayload>({
    mutationFn: createManualTrade,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.pendingPositions });
      queryClient.invalidateQueries({ queryKey: queryKeys.dashboardStats });
      // The ledger always gains a row, even when the fill opens rather than
      // closes a position — which is the case the inbox cannot show.
      queryClient.invalidateQueries({ queryKey: queryKeys.trades });
      // A closing fill also produces a round trip immediately (the backend
      // re-runs FIFO matching on save), so the journal and every analytics
      // card built on it need to refresh too, not just the ledger's raw list.
      queryClient.invalidateQueries({ queryKey: queryKeys.roundTrips });
      queryClient.invalidateQueries({ queryKey: queryKeys.advancedMetrics });
    },
  });
}

/**
 * Trade plans. Defaults to OPEN — the only status you can act on.
 *
 * Deliberately does NOT invalidate anything on the dashboard: a plan is not an
 * execution, so nothing about it can move P&L, win rate or exposure.
 */
export function usePlans(status: PlanStatus | 'ALL' = 'OPEN') {
  return useQuery<TradePlan[], Error>({
    queryKey: queryKeys.plans(status),
    queryFn: () => getPlans(status),
  });
}

/**
 * Record a trade you intend to take.
 *
 * Only the plan lists are invalidated. If creating a plan ever caused the
 * dashboard to change, that would be the bug this whole feature exists to
 * prevent — so the absence of those invalidations is deliberate, not an
 * oversight.
 */
export function useCreatePlan() {
  const queryClient = useQueryClient();

  return useMutation<TradePlan, Error, TradePlanPayload>({
    mutationFn: createPlan,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.plansRoot });
    },
  });
}

/** Edit a plan that has not been attached yet. */
export function useUpdatePlan() {
  const queryClient = useQueryClient();

  return useMutation<
    TradePlan,
    Error,
    { planId: string; payload: TradePlanUpdatePayload }
  >({
    mutationFn: ({ planId, payload }) => updatePlan(planId, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.plansRoot });
    },
  });
}

/** Cancel a plan you did not take. It stays in the record as CANCELLED. */
export function useCancelPlan() {
  const queryClient = useQueryClient();

  return useMutation<TradePlan, Error, string>({
    mutationFn: cancelPlan,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.plansRoot });
    },
  });
}

/**
 * The chart screenshot attached to a plan, as a URL an <img> can use.
 *
 * The endpoint needs the Clerk bearer token, which an <img src> cannot carry,
 * so the bytes are fetched through the authenticated client and wrapped in an
 * object URL. That URL owns memory until it is revoked, which is what the
 * effect below is for — without it, every ledger row opened would leak the
 * full image for the lifetime of the tab.
 */
export function usePlanChart(planId: string | null, enabled = true) {
  const query = useQuery<Blob>({
    queryKey: queryKeys.planChart(planId ?? ''),
    queryFn: () => getPlanChart(planId as string),
    enabled: Boolean(planId) && enabled,
    // The image for a given plan does not change unless the user replaces it,
    // and doing so invalidates this key explicitly.
    staleTime: Infinity,
    gcTime: 10 * 60 * 1000,
  });

  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!query.data) {
      setUrl(null);
      return;
    }
    const objectUrl = URL.createObjectURL(query.data);
    setUrl(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [query.data]);

  return { url, isLoading: query.isLoading, error: query.error };
}

/** Attach or replace a plan's chart screenshot. */
export function useUploadPlanChart() {
  const queryClient = useQueryClient();

  return useMutation<TradePlan, Error, { planId: string; image: Blob; filename: string }>({
    mutationFn: ({ planId, image, filename }) => uploadPlanChart(planId, image, filename),
    onSuccess: (_plan, { planId }) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.plansRoot });
      // The cached blob is now the previous screenshot.
      queryClient.invalidateQueries({ queryKey: queryKeys.planChart(planId) });
      // The ledger renders the chart beside the post-mortem of an attached
      // plan, so its copy of the plan is stale too.
      queryClient.invalidateQueries({ queryKey: queryKeys.roundTrips });
    },
  });
}

/** Remove a plan's chart, keeping the plan. */
export function useDeletePlanChart() {
  const queryClient = useQueryClient();

  return useMutation<TradePlan, Error, string>({
    mutationFn: deletePlanChart,
    onSuccess: (_plan, planId) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.plansRoot });
      queryClient.removeQueries({ queryKey: queryKeys.planChart(planId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.roundTrips });
    },
  });
}

/**
 * Link a plan to a fill the sync did not match on its own.
 *
 * This one DOES touch the ledger: attaching copies the plan's stop, target and
 * sizing onto the opening fill, so the round trip's planned R changes.
 */
export function useAttachPlan() {
  const queryClient = useQueryClient();

  return useMutation<PlanAttachResult, Error, { tradeId: string; planId: string }>({
    mutationFn: ({ tradeId, planId }) => attachPlan(tradeId, planId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.plansRoot });
      queryClient.invalidateQueries({ queryKey: queryKeys.roundTrips });
      queryClient.invalidateQueries({ queryKey: queryKeys.trades });
    },
  });
}

/**
 * Unlink a wrongly attached plan, returning it to OPEN.
 *
 * The values it copied stay on the trade. They may have been edited since, and
 * nothing distinguishes an untouched copy from a corrected one — so clearing
 * them could silently discard the user's own work.
 */
export function useDetachPlan() {
  const queryClient = useQueryClient();

  return useMutation<PlanDetachResult, Error, string>({
    mutationFn: detachPlan,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.plansRoot });
      queryClient.invalidateQueries({ queryKey: queryKeys.roundTrips });
      queryClient.invalidateQueries({ queryKey: queryKeys.trades });
    },
  });
}

/** Create a strategy, then refresh every list that offers strategies. */
export function useCreateStrategy() {
  const queryClient = useQueryClient();

  return useMutation<Strategy, Error, StrategyCreatePayload>({
    mutationFn: createStrategy,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.strategies });
    },
  });
}

export interface UpdateStrategyVariables {
  id: string;
  payload: StrategyUpdatePayload;
}

/** Save playbook edits for an existing strategy. */
export function useUpdateStrategy() {
  const queryClient = useQueryClient();

  return useMutation<Strategy, Error, UpdateStrategyVariables>({
    mutationFn: ({ id, payload }) => updateStrategy(id, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.strategies });
    },
  });
}

/**
 * Delete a playbook entry, having first moved its history elsewhere.
 *
 * Invalidates far more than the strategy list: reassignment rewrites
 * `strategy_id` on trades, positions and plans, so every surface that groups
 * by strategy — the analytics breakdown above all — is now stale.
 */
export function useDeleteStrategy() {
  const queryClient = useQueryClient();

  return useMutation<
    StrategyDeleteResult,
    Error,
    { id: string; reassignTo?: string | null }
  >({
    mutationFn: ({ id, reassignTo }) => deleteStrategy(id, reassignTo),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.strategies });
      queryClient.invalidateQueries({ queryKey: queryKeys.trades });
      queryClient.invalidateQueries({ queryKey: queryKeys.roundTrips });
      queryClient.invalidateQueries({ queryKey: queryKeys.advancedMetrics });
      queryClient.invalidateQueries({ queryKey: queryKeys.plansRoot });
    },
  });
}

export interface ReviewMutationVariables {
  id: string;
  payload: PositionReviewPayload;
}

/**
 * Submit a review checklist for one position.
 *
 * On success both the pending queue and the dashboard are invalidated: the
 * reviewed position leaves the queue, and its newly attached strategy/tags
 * change what the analytics endpoint reports. Invalidating only the queue
 * would leave a stale heatmap on screen.
 */
export function useReviewPosition() {
  const queryClient = useQueryClient();

  return useMutation<Position, Error, ReviewMutationVariables>({
    mutationFn: ({ id, payload }) => updatePositionReview(id, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.pendingPositions });
      queryClient.invalidateQueries({ queryKey: queryKeys.dashboardStats });
      // Newly attached mistake tags change the per-mistake breakdown.
      queryClient.invalidateQueries({ queryKey: queryKeys.advancedMetrics });
      // The journal renders the review inline, so it must not show stale text.
      queryClient.invalidateQueries({ queryKey: queryKeys.roundTrips });
    },
  });
}

/**
 * Delete an execution, and rebuild the round trips it belonged to.
 *
 * The server does the rebuilding — a position's size and P&L are derived from
 * a specific set of fills, so removing one without re-matching leaves a round
 * trip asserting a size its remaining fills cannot support. The result reports
 * how many positions were rebuilt and how many reviews that discarded.
 */
export function useDeleteTrade() {
  const queryClient = useQueryClient();
  return useMutation<TradeDeleteResult, Error, string>({
    mutationFn: deleteTrade,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.trades });
      queryClient.invalidateQueries({ queryKey: queryKeys.roundTrips });
      queryClient.invalidateQueries({ queryKey: queryKeys.dashboardStats });
      queryClient.invalidateQueries({ queryKey: queryKeys.advancedMetrics });
      queryClient.invalidateQueries({ queryKey: queryKeys.pendingPositions });
    },
  });
}

/**
 * Everything a correction can move, invalidated together.
 *
 * Editing or removing one fill re-runs FIFO for its ticker, which can dissolve
 * or create round trips — so P&L, R-multiples and the review queue all shift,
 * not just the row that was touched.
 */
function invalidateLedger(queryClient: ReturnType<typeof useQueryClient>) {
  queryClient.invalidateQueries({ queryKey: queryKeys.trades });
  queryClient.invalidateQueries({ queryKey: queryKeys.roundTrips });
  queryClient.invalidateQueries({ queryKey: queryKeys.dashboardStats });
  queryClient.invalidateQueries({ queryKey: queryKeys.advancedMetrics });
  queryClient.invalidateQueries({ queryKey: queryKeys.pendingPositions });
}

/** Correct the facts of a fill — quantity, price, side or time. */
export function useUpdateExecution() {
  const queryClient = useQueryClient();
  return useMutation<
    ExecutionUpdateResult,
    Error,
    { id: string; payload: ExecutionUpdatePayload }
  >({
    mutationFn: ({ id, payload }) => updateExecution(id, payload),
    onSuccess: () => invalidateLedger(queryClient),
  });
}

/** Remove a round trip and the executions under it — for a trade that never happened. */
export function useDeletePosition() {
  const queryClient = useQueryClient();
  return useMutation<
    PositionDeleteResult,
    Error,
    { id: string; reason?: string; includeShared?: boolean }
  >({
    mutationFn: ({ id, reason, includeShared }) =>
      deletePosition(id, reason, includeShared),
    onSuccess: () => invalidateLedger(queryClient),
  });
}

/**
 * What deleting this round trip would take with it, fetched when the
 * confirmation opens.
 *
 * Disabled until an id is passed, so nothing is requested while the dialog is
 * closed. Not cached beyond the interaction: the answer depends on which
 * executions currently exist, and the next sync can change it.
 */
export function usePositionDeleteImpact(positionId: string | null) {
  return useQuery<PositionDeleteImpact>({
    queryKey: ['position-delete-impact', positionId],
    queryFn: () => getPositionDeleteImpact(positionId as string),
    enabled: positionId !== null,
    gcTime: 0,
    staleTime: 0,
    retry: false,
  });
}

/** Take a round trip out of the queue without reviewing it. Keeps the P&L. */
export function useDismissPosition() {
  const queryClient = useQueryClient();
  return useMutation<Position, Error, string>({
    mutationFn: dismissPosition,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.pendingPositions });
      // The round trip itself is unchanged, but its review_status drives the
      // badge the ledger renders.
      queryClient.invalidateQueries({ queryKey: queryKeys.roundTrips });
    },
  });
}

/** Dismiss the sync summary without erasing the fact that a sync happened. */
export function useAcknowledgeSync() {
  const queryClient = useQueryClient();
  return () =>
    queryClient.setQueryData<LastSyncState>(queryKeys.lastSync, (prev) =>
      prev ? { ...prev, acknowledged: true } : prev
    );
}

/** Broker fills ingest is deliberately skipping. */
export function useSuppressedExecutions() {
  return useQuery<SuppressedExecution[]>({
    queryKey: queryKeys.suppressed,
    queryFn: getSuppressedExecutions,
  });
}

/**
 * Lift a tombstone so a future sync may re-import the fill.
 *
 * Invalidates the ledger queries even though nothing changes yet: the fill
 * does not return until a sync covers its date. That is deliberate — if a
 * sync runs in the same session the numbers must not be served from a cache
 * populated while the fill was still suppressed.
 */
export function useUnsuppressTrade() {
  const queryClient = useQueryClient();
  return useMutation<UnsuppressResult, Error, string>({
    mutationFn: unsuppressExecution,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.suppressed });
      queryClient.invalidateQueries({ queryKey: queryKeys.roundTrips });
      queryClient.invalidateQueries({ queryKey: queryKeys.dashboardStats });
    },
  });
}

/** Disciplines list for review checklist. */
export function useDisciplines() {
  return useQuery<Discipline[]>({
    queryKey: queryKeys.disciplines,
    queryFn: getDisciplines,
    staleTime: 5 * 60_000,
  });
}

/** Add a new discipline rule. */
export function useCreateDiscipline() {
  const queryClient = useQueryClient();
  return useMutation<Discipline, Error, DisciplineCreatePayload>({
    mutationFn: createDiscipline,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.disciplines });
    },
  });
}

/** Delete a discipline rule. */
export function useDeleteDiscipline() {
  const queryClient = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: deleteDiscipline,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.disciplines });
    },
  });
}

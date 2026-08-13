'use client';

import { useEffect, useMemo, useState } from 'react';
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
  advancedMetrics: ['advancedMetrics'] as const,
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
export function useAdvancedMetrics() {
  return useQuery<AdvancedMetrics>({
    queryKey: queryKeys.advancedMetrics,
    queryFn: getAdvancedMetrics,
  });
}

/**
 * Pull executions from IBKR via the idempotent ingest pipeline.
 *
 * Invalidation lives here rather than in the button so any caller gets a
 * correct cache refresh: the ingest promotes staged fills into `trades` and
 * re-runs FIFO matching, so new positions can appear in the inbox, shift
 * every dashboard figure, and change what a mounted Trade Ledger or
 * analytics card is already showing.
 */
export function useSyncBroker() {
  const queryClient = useQueryClient();

  return useMutation<IngestResult, Error, void>({
    mutationFn: ingestIBKR,
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.pendingPositions });
      queryClient.invalidateQueries({ queryKey: queryKeys.dashboardStats });
      // A sync promotes fills into trades and re-runs FIFO matching, so the
      // ledger and every analytics card built on it go stale the same way an
      // annotation or a manual entry already does.
      queryClient.invalidateQueries({ queryKey: queryKeys.trades });
      queryClient.invalidateQueries({ queryKey: queryKeys.roundTrips });
      queryClient.invalidateQueries({ queryKey: queryKeys.advancedMetrics });
      // The server just recorded this run, so the durable record the badge
      // reads is now a version behind.
      queryClient.invalidateQueries({ queryKey: queryKeys.syncStatus });

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

/**
 * The last sync anyone performed, from the server rather than this tab.
 *
 * This is what makes a scheduled run visible: `useLastSync` below can only
 * report syncs THIS browser session ran, so before this a nightly job that
 * failed on an expired token produced the same silence as a quiet market.
 *
 * Not polled. A daily schedule does not warrant a timer on every mounted
 * page, and the value refetches on mount and whenever a sync completes, which
 * covers every moment it visibly changes.
 */
export function useSyncStatus() {
  return useQuery<SyncStatus, Error>({
    queryKey: queryKeys.syncStatus,
    queryFn: getSyncStatus,
  });
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

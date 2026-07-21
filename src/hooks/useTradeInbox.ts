'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  createManualTrade,
  getAdvancedMetrics,
  ingestIBKR,
  createStrategy,
  getDashboardAnalytics,
  annotateTrade,
  getPendingPositions,
  getPositionFills,
  getRoundTrips,
  getStrategies,
  getTrades,
  updatePositionReview,
  updateStrategy,
} from '@/lib/api';
import type {
  AdvancedMetrics,
  DashboardStats,
  IngestResult,
  ManualTradePayload,
  ManualTradeResult,
  Position,
  PositionFill,
  PositionReviewPayload,
  RoundTrip,
  Strategy,
  StrategyCreatePayload,
  StrategyUpdatePayload,
  Trade,
  TradeAnnotationPayload,
} from '@/types/api';

/**
 * Query keys, centralized so a hook and its invalidator can never drift apart.
 */
export const queryKeys = {
  pendingPositions: ['positions', 'pending'] as const,
  trades: ['trades'] as const,
  roundTrips: ['roundTrips'] as const,
  positionFills: (id: string) => ['positions', id, 'fills'] as const,
  strategies: ['strategies'] as const,
  dashboardStats: ['dashboardStats'] as const,
  advancedMetrics: ['advancedMetrics'] as const,
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

/** The journal: one row per trade idea, open or closed. */
export function useRoundTrips() {
  return useQuery<RoundTrip[]>({
    queryKey: queryKeys.roundTrips,
    queryFn: () => getRoundTrips(),
  });
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

/** Strategies for the review dropdown. Rarely changes, so cache it longer. */
export function useStrategies() {
  return useQuery<Strategy[]>({
    queryKey: queryKeys.strategies,
    queryFn: getStrategies,
    staleTime: 5 * 60_000,
  });
}

/** Core stats + heatmap grid backing the dashboard. */
export function useDashboardStats() {
  return useQuery<DashboardStats>({
    queryKey: queryKeys.dashboardStats,
    queryFn: getDashboardAnalytics,
  });
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
 * re-runs FIFO matching, so new positions can appear in the inbox and shift
 * every dashboard figure.
 */
export function useSyncBroker() {
  const queryClient = useQueryClient();

  return useMutation<IngestResult, Error, void>({
    mutationFn: ingestIBKR,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.pendingPositions });
      queryClient.invalidateQueries({ queryKey: queryKeys.dashboardStats });
    },
  });
}

/**
 * Hand-log an execution.
 *
 * The backend re-runs FIFO matching on save, so a closing fill can produce a
 * new position immediately. Both the inbox queue and the dashboard are
 * invalidated so the queue and heatmap reflect it without a reload.
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

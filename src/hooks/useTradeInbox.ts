'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  createManualTrade,
  getAdvancedMetrics,
  getTradeReviewQueue,
  reviewTrade,
  ingestIBKR,
  createStrategy,
  getDashboardAnalytics,
  getPendingPositions,
  getStrategies,
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
  PositionReviewPayload,
  Strategy,
  StrategyCreatePayload,
  StrategyUpdatePayload,
  TradeReview,
  TradeReviewPayload,
  TradeReviewStatus,
} from '@/types/api';

/**
 * Query keys, centralized so a hook and its invalidator can never drift apart.
 */
export const queryKeys = {
  pendingPositions: ['positions', 'pending'] as const,
  strategies: ['strategies'] as const,
  dashboardStats: ['dashboardStats'] as const,
  advancedMetrics: ['advancedMetrics'] as const,
  tradeReviewQueue: (status: TradeReviewStatus) =>
    ['trades', 'review-queue', status] as const,
};

/** Positions awaiting review — the Trade Inbox queue. */
export function usePendingPositions() {
  return useQuery<Position[]>({
    queryKey: queryKeys.pendingPositions,
    queryFn: getPendingPositions,
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

/** Executions awaiting the qualitative review pass. */
export function useTradeReviewQueue(status: TradeReviewStatus = 'pending') {
  return useQuery<TradeReview[]>({
    queryKey: queryKeys.tradeReviewQueue(status),
    queryFn: () => getTradeReviewQueue(status),
  });
}

export interface TradeReviewVariables {
  id: string;
  payload: TradeReviewPayload;
}

/**
 * Save a qualitative review.
 *
 * Invalidates both review queues (the trade leaves 'pending' and joins
 * 'reviewed') and the advanced metrics, since newly attached mistake tags
 * change the per-mistake breakdown.
 */
export function useReviewTrade() {
  const queryClient = useQueryClient();

  return useMutation<TradeReview, Error, TradeReviewVariables>({
    mutationFn: ({ id, payload }) => reviewTrade(id, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: queryKeys.tradeReviewQueue('pending'),
      });
      queryClient.invalidateQueries({
        queryKey: queryKeys.tradeReviewQueue('reviewed'),
      });
      queryClient.invalidateQueries({ queryKey: queryKeys.advancedMetrics });
    },
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
    },
  });
}

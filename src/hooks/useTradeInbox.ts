'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  getDashboardAnalytics,
  getPendingPositions,
  getStrategies,
  updatePositionReview,
} from '@/lib/api';
import type {
  DashboardStats,
  Position,
  PositionReviewPayload,
  Strategy,
} from '@/types/api';

/**
 * Query keys, centralized so a hook and its invalidator can never drift apart.
 */
export const queryKeys = {
  pendingPositions: ['positions', 'pending'] as const,
  strategies: ['strategies'] as const,
  dashboardStats: ['dashboardStats'] as const,
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

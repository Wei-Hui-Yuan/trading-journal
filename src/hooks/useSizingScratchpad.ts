'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  createSizingEntry,
  deleteSizingEntry,
  getSizingScratchpad,
  promoteSizingEntry,
  updateSizingEntry,
} from '@/lib/api';
import type { TradePlan } from '@/types/api';
import type {
  SizingEntryPayload,
  SizingEntryUpdatePayload,
  SizingScratchpadEntry,
} from '@/types/sizing';

import { queryKeys } from './useTradeInbox';

/** Kept under its own root, entirely apart from `queryKeys` above it, since
 * nothing in the scratchpad is read by anything the trading journal caches --
 * the one exception, promotion, invalidates `queryKeys.plansRoot` directly
 * rather than sharing a key with it. */
export const sizingKeys = {
  root: ['sizingScratchpad'] as const,
};

/**
 * Every note from the last three days, newest first.
 *
 * Inherits the app-wide defaults from QueryProvider — `staleTime: 30_000` and
 * `refetchOnWindowFocus: false`. This comment used to claim the opposite, that
 * `staleTime: 0` was deliberate so a note would re-fetch on every focus;
 * neither half was true, and the behaviour it described is the slower one.
 *
 * The defaults are right for this list anyway. It is edited from one tab at a
 * time, and every mutation below invalidates the root, so a commit refreshes
 * what changed without every window focus paying for a round trip.
 */
export function useSizingScratchpad() {
  return useQuery<SizingScratchpadEntry[], Error>({
    queryKey: sizingKeys.root,
    queryFn: getSizingScratchpad,
  });
}

export function useCreateSizingEntry() {
  const queryClient = useQueryClient();
  return useMutation<SizingScratchpadEntry, Error, SizingEntryPayload>({
    mutationFn: createSizingEntry,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: sizingKeys.root });
    },
  });
}

export function useUpdateSizingEntry() {
  const queryClient = useQueryClient();
  return useMutation<
    SizingScratchpadEntry,
    Error,
    { id: string; payload: SizingEntryUpdatePayload }
  >({
    mutationFn: ({ id, payload }) => updateSizingEntry(id, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: sizingKeys.root });
    },
  });
}

export function useDeleteSizingEntry() {
  const queryClient = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: deleteSizingEntry,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: sizingKeys.root });
    },
  });
}

/**
 * Turn a note into a real plan.
 *
 * Invalidates BOTH roots, because the one action reaches into both domains:
 * the note is gone (`sizingKeys.root`) and a new OPEN plan exists
 * (`queryKeys.plansRoot`) — the Plan dock and the journal need to see it
 * exactly as if it had been entered through the Plan modal.
 */
export function usePromoteSizingEntry() {
  const queryClient = useQueryClient();
  return useMutation<TradePlan, Error, string>({
    mutationFn: promoteSizingEntry,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: sizingKeys.root });
      queryClient.invalidateQueries({ queryKey: queryKeys.plansRoot });
    },
  });
}
